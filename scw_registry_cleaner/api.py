from __future__ import annotations

import logging
import platform
import pprint
import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from itertools import islice
from logging import StreamHandler
from typing import TYPE_CHECKING, TypeVar

import anyio
from anyio import Lock, create_task_group
from httpx import (
    AsyncClient,
    AsyncHTTPTransport,
    HTTPStatusError,
    Request,
    Response,
    Timeout,
)

import scw_registry_cleaner

if TYPE_CHECKING:
    from collections.abc import Generator, Iterable
    from re import Pattern
    from typing import Any

__all__ = ["RegistryAPI", "Tag", "TagStatus"]

BatchedT = TypeVar("BatchedT")

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] {%(filename)s:%(lineno)d} %(levelname)s - %(message)s",
    handlers=[StreamHandler(stream=sys.stdout)],
)
# Filter out httpx logging
httpx_logger = logging.getLogger("httpx")
httpx_logger.setLevel(logging.WARN)
logger = logging.getLogger(__name__)

REGIONS = {
    "fr-par": {
        "url": "https://api.scaleway.com/registry/v1/regions/fr-par",
    },
    "nl-ams": {
        "url": "https://api.scaleway.com/registry/v1/regions/nl-ams",
    },
    "pl-waw": {
        "url": "https://api.scaleway.com/registry/v1/regions/pl-waw",
    },
}


def batched(
    iterable: Iterable[BatchedT], n: int
) -> Generator[tuple[BatchedT], Any, None]:
    "Batch data into tuples of length n. The last batch may be shorter."
    # batched('ABCDEFG', 3) --> ABC DEF G
    if n < 1:
        raise ValueError("n must be at least one")
    it = iter(iterable)
    while batch := tuple(islice(it, n)):
        yield batch


class TagStatus(Enum):
    UNKNOWN = "unknown"
    READY = "ready"
    DELETING = "deleting"
    ERROR = "error"
    LOCKED = "locked"


@dataclass(frozen=True)
class Tag:
    id: str
    name: str
    created_at: datetime
    full_name: str
    status: TagStatus

    def __str__(self) -> str:
        return f"status: {self.status.value}\t{self.full_name}"


class _CustomTransport(AsyncHTTPTransport):
    # Maximum number of times we try to make a request against an API in
    # maintenance before aborting.
    MAX_RETRIES = 3

    def __init__(self, debug: bool, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.logging = debug

    def retry_in(self, retry):
        """If the API returns a maintenance HTTP status code, sleep a while
        before retrying.
        """
        return min(2**retry, 30)

    async def handle_async_request(self, request: Request) -> Response:
        retry = 0

        while True:
            try:
                resp = await super().handle_async_request(request)
                if self.logging:
                    pprint.pprint(resp.json())
                return resp
            except HTTPStatusError as exc:
                # Not a maintenance exception
                if exc.response.status_code not in (502, 503, 504):
                    raise

                retry += 1
                retry_in = self.retry_in(retry)

                if retry >= self.MAX_RETRIES:
                    logger.error(
                        f"API endpoint still in maintenance after"
                        f"{self.MAX_RETRIES} attempts. "
                        f"Stop trying."
                    )
                    raise

                logger.info(
                    f"API endpoint is currently in maintenance. Try again in "
                    f"{retry_in} seconds... (retry {retry} on {self.MAX_RETRIES})"
                )
                await anyio.sleep(retry_in)


class RegistryAPI:
    """The default region is par1 as it was the first availability zone
    provided by Scaleway, but it could change in the future.
    """

    base_url = None
    user_agent = "scw-sdk/{} Python/{} {}".format(
        scw_registry_cleaner.__version__,
        " ".join(sys.version.split()),
        platform.platform(),
    )

    def __init__(
        self,
        auth_token: str | None = None,
        auth_jwt: str | None = None,
        user_agent: str | None = None,
        verify_ssl: bool = True,
        region: str | None = None,
        base_url: str | None = None,
        debug: bool = False,
    ):
        if base_url:
            self.base_url = base_url

        assert (
            region is None or base_url is None
        ), "Specify either region or base_url, not both."

        self.region = region or "fr-par"

        assert self.region in REGIONS, f"{self.region} is not a valid Scaleway region."

        self.auth_token = auth_token
        self.auth_jwt = auth_jwt

        if user_agent is not None:
            self.user_agent = user_agent

        self.verify_ssl = verify_ssl

        self.base_url = REGIONS[self.region]["url"]
        self._read_timout: float = 20.0
        self.client = self.make_client(debug)

    def make_client(self, logging: bool) -> AsyncClient:
        """Attaches headers needed to query Scaleway APIs."""
        client = AsyncClient(
            mounts={"https://": _CustomTransport(debug=logging)},
            timeout=Timeout(5.0, read=self._read_timout),
        )

        client.headers.update({"User-Agent": self.user_agent})

        if self.auth_token:
            client.headers.update({"X-Auth-Token": self.auth_token})
        if self.auth_jwt:
            client.headers.update({"X-Session-Token": self.auth_jwt})

        return client

    @classmethod
    def to_tag(cls, namespace: str, image: dict, data: dict) -> Tag:
        created_at_dt = datetime.fromisoformat(data["created_at"].replace("Z", ""))
        return Tag(
            id=data["id"],
            name=data["name"],
            created_at=created_at_dt,
            full_name=f"{namespace}/{image['name']}:{data['name']}",
            status=TagStatus(data["status"]),
        )

    async def _task_filter_tags(
        self,
        lock: Lock,
        shared_tags: dict[str, list[Tag]],
        namespace: str,
        image: dict,
        pattern: Pattern,
    ) -> None:
        tags = await self.get_image_tags(image["id"])
        for t in tags:
            if pattern is None or pattern.match(t["name"]):
                tag = self.to_tag(namespace, image, t)
                async with lock:
                    shared_tags[image["name"]].append(tag)

    async def get_namespace(self, name: str | None = None) -> dict:
        resp = await self.client.get(
            f"{self.base_url}/namespaces", params={"name": name}
        )
        return resp.json()["namespaces"]

    async def get_images(self, namespace_id: str, name: str | None = None) -> dict:
        params = {"namespace_id": namespace_id}
        if name:
            params["name"] = name
        resp = await self.client.get(f"{self.base_url}/images", params=params)
        return resp.json()["images"]

    async def get_image_tags(self, image_id: str, max_pages: int = 20) -> list:
        page: int = 1
        tags: list[Tag] = []
        while page < max_pages:
            resp = await self.client.get(
                f"{self.base_url}/images/{image_id}/tags",
                params={"page_size": 100, "page": page},
            )
            data = resp.json()
            page += 1
            tags.extend(data["tags"])
        return tags

    async def filter_namespace_tags(
        self, namespace: str, pattern: Pattern
    ) -> dict[str, list[Tag]]:
        selected_tags: defaultdict[str, list[Tag]] = defaultdict(list)

        resp = await self.get_namespace(name=namespace)
        namespace_id = resp[0]["id"]
        images = await self.get_images(namespace_id)
        lock = Lock()
        async with create_task_group() as tg:
            for image in images:
                tg.start_soon(
                    self._task_filter_tags,
                    lock,
                    selected_tags,
                    namespace,
                    image,
                    pattern,
                )

        return selected_tags

    async def delete_tag(self, id: str) -> dict:
        resp = await self.client.delete(f"{self.base_url}/tags/{id}")
        logger.info(f"Deleted tag {id}")
        return resp.json()

    async def bulk_delete_tag(self, ids: list[str]) -> None:
        for batch in batched(ids, round(self._read_timout / 2)):
            async with create_task_group() as tg:
                for tag_id in batch:
                    tg.start_soon(self.delete_tag, tag_id)

    async def get_old_tags(
        self,
        namespace: str,
        grace: timedelta | None = None,
        keep: int | None = None,
        pattern: Pattern | None = None,
    ) -> dict[str, list[Tag]]:
        selected_tags = await self.filter_namespace_tags(
            namespace, pattern or re.compile(".*")
        )
        tags_to_delete: defaultdict[str, list[Tag]] = defaultdict(list)

        keep_ = keep or float("-inf")
        for image, tags in selected_tags.items():
            tags.sort(key=lambda item: item.created_at)
            to_delete, old_tags = [], []
            now = datetime.now()
            for t in tags:
                too_old = False
                if grace:
                    too_old = now - t.created_at >= grace
                if too_old:
                    old_tags.append(t)
                    if len(old_tags) - len(to_delete) >= keep_:
                        to_delete.append(t)
            tags_to_delete[image] = to_delete
        return tags_to_delete
