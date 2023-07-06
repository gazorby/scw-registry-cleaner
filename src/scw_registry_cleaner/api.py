"""Minimal client for scaleway registry."""

from __future__ import annotations

import logging
import platform
import pprint
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from itertools import islice
from typing import TYPE_CHECKING, Self, TypeVar

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

__all__ = ["ImageRef", "RegistryAPI", "TagStatus"]

BatchedT = TypeVar("BatchedT")

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
    iterable: Iterable[BatchedT], batch_size: int
) -> Generator[tuple[BatchedT, ...], Any, None]:
    "Batch data into tuples of length n. The last batch may be shorter."
    # batched('ABCDEFG', 3) --> ABC DEF G
    if batch_size < 1:
        raise ValueError("batch size must be at least one")
    iterator = iter(iterable)
    while batch := tuple(islice(iterator, batch_size)):
        yield batch


class TagStatus(Enum):
    """Image tag status."""

    UNKNOWN = "unknown"
    READY = "ready"
    DELETING = "deleting"
    ERROR = "error"
    LOCKED = "locked"


@dataclass(frozen=True)
class ImageRef:
    """Represent image reference in the registry."""

    tag_id: str
    tag_name: str
    created_at: datetime
    status: TagStatus
    image_name: str
    namespace: str

    @property
    def ref(self) -> str:
        """Return a fully qualified image reference."""
        return f"{self.namespace}/{self.image_name}:{self.tag_name}"

    def age(self) -> tuple[int, int]:
        """Return image age in days and hours

        Returns:
            A tuple of integers
        """
        delta = datetime.now() - self.created_at
        hours, _ = divmod(delta.seconds, 3600)
        return delta.days, hours


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
                        (
                            "API endpoint still in maintenance after"
                            " %s attempts. Stop trying."
                        ),
                        self.MAX_RETRIES,
                    )
                    raise

                logger.info(
                    (
                        "API endpoint is currently in maintenance. Try again in "
                        "%s seconds... (retry %s on %s)"
                    ),
                    retry_in,
                    retry,
                    self.MAX_RETRIES,
                )
                await anyio.sleep(retry_in)


class RegistryAPI:
    """The default region is par1 as it was the first availability zone
    provided by Scaleway, but it could change in the future.
    """

    base_url = None
    user_agent = (
        f"scw-sdk/{scw_registry_cleaner.__version__}"
        f" Python/{' '.join(sys.version.split())} {platform.platform()}"
    )

    def __init__(
        self,
        token: str | None = None,
        auth_jwt: str | None = None,
        user_agent: str | None = None,
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

        self.auth_token = token
        self.auth_jwt = auth_jwt

        if user_agent is not None:
            self.user_agent = user_agent

        self.base_url = REGIONS[self.region]["url"]
        self._read_timout: float = 20.0
        self.client = self._make_client(debug)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.client.aclose()

    def _make_client(self, debug: bool) -> AsyncClient:
        """Create the httpx client.

        Args:
            logging: Enable logging on the HTTP transport

        Returns:
            AN AsyncClient instance
        """
        client = AsyncClient(
            mounts={"https://": _CustomTransport(debug=debug)},
            timeout=Timeout(5.0, read=self._read_timout),
        )

        client.headers.update({"User-Agent": self.user_agent})

        if self.auth_token:
            client.headers.update({"X-Auth-Token": self.auth_token})
        if self.auth_jwt:
            client.headers.update({"X-Session-Token": self.auth_jwt})

        return client

    @classmethod
    def to_image_ref(cls, namespace: str, image: dict, tag: dict) -> ImageRef:
        """Build an ImageRef from image and tag.

        Args:
            namespace: Registry namespace
            image: Image API object
            data: Tag API object

        Returns:
            An ImageRef instance
        """
        created_at_dt = datetime.fromisoformat(tag["created_at"].replace("Z", ""))
        return ImageRef(
            tag_id=tag["id"],
            tag_name=tag["name"],
            created_at=created_at_dt,
            status=TagStatus(tag["status"]),
            image_name=image["name"],
            namespace=namespace,
        )

    async def _task_get_tags(
        self,
        lock: Lock,
        shared_tags: dict[str, list[ImageRef]],
        namespace: str,
        image: dict,
    ) -> None:
        """An async task that filter image tags based on provided pattern

        Args:
            lock: Anyio lock to prevent concurrent access to shared_tags
            shared_tags: Shared dictionary on which matching tags will be added
            namespace: Registry namespace
            image: Namespace image
            pattern: Pattern to search for
        """
        tags = await self.get_image_tags(image["id"])
        for tag in tags:
            tag = self.to_image_ref(namespace, image, tag)
            async with lock:
                shared_tags[image["name"]].append(tag)

    async def get_namespace(self, name: str) -> dict:
        """Get a registry namespace

        Args:
            name: Namespace name.

        Returns:
            Scaleway API response
        """
        resp = await self.client.get(
            f"{self.base_url}/namespaces", params={"name": name}
        )
        return resp.json()["namespaces"]

    async def get_images(self, namespace_id: str, name: str | None = None) -> dict:
        """Get namespace images.

        Args:
            namespace_id: Id of the namespace to list images from
            name: Image name to filter on. Defaults to None.

        Returns:
            Scaleway API response
        """
        params = {"namespace_id": namespace_id}
        if name:
            params["name"] = name
        resp = await self.client.get(f"{self.base_url}/images", params=params)
        return resp.json()["images"]

    async def get_image_tags(self, image_id: str, max_pages: int = 20) -> list:
        """Get tags for the provided image id.

        Args:
            image_id: Id of the image to list tag from
            max_pages: Maximum number of pages to retrieve when listing tags.
                Defaults to 20.

        Returns:
            Scaleway API response
        """
        page: int = 1
        tags: list[ImageRef] = []
        while page < max_pages:
            resp = await self.client.get(
                f"{self.base_url}/images/{image_id}/tags",
                params={"page_size": 100, "page": page},
            )
            data = resp.json()
            page += 1
            tags.extend(data["tags"])
        return tags

    async def get_namespace_tags(self, namespace: str) -> dict[str, list[ImageRef]]:
        """Filter all tags on the given namespace using the provided pattern.

        Args:
            namespace: Name of the namespace in which to filter tags
            pattern: Regex pattern used to match tag names

        Returns:
            A Mapping of image names to ImageRef list
        """
        selected_tags: defaultdict[str, list[ImageRef]] = defaultdict(list)

        resp = await self.get_namespace(name=namespace)
        namespace_id = resp[0]["id"]
        images = await self.get_images(namespace_id)
        lock = Lock()
        async with create_task_group() as task_group:
            for image in images:
                task_group.start_soon(
                    self._task_get_tags,
                    lock,
                    selected_tags,
                    namespace,
                    image,
                )

        return selected_tags

    async def delete_tag(self, tag_id: str) -> dict:
        """Delete the specified tag

        Args:
            id: Id of the tag to delete

        Returns:
            Scaleway API response
        """
        resp = await self.client.delete(f"{self.base_url}/tags/{tag_id}")
        logger.info("Deleted tag %s", tag_id)
        return resp.json()

    async def bulk_delete_tag(self, ids: list[str]) -> None:
        """Delete image tags in bulk

        Args:
            ids: Ids of the tags to delete
        """
        for batch in batched(ids, round(self._read_timout / 2)):
            async with create_task_group() as task_group:
                for tag_id in batch:
                    task_group.start_soon(self.delete_tag, tag_id)

    async def get_old_tags(
        self,
        namespace: str,
        grace: timedelta | None = None,
        keep: int | None = None,
        pattern: Pattern[str] | None = None,
        exclude_statuses: Iterable[TagStatus] | None = None,
    ) -> dict[str, list[ImageRef]]:
        """Get tag older then the specified age

        Args:
            namespace: Name of the namespace in which to get tags
            grace: Minimal age tag age. Defaults to None.
            keep: Minimal number of tags to keep per image. Defaults to None.
            pattern: Regex pattern that must match tag names. Defaults to None.

        Returns:
            A Mapping of image names to ImageRef list
        """
        selected_tags = await self.get_namespace_tags(namespace)
        tags_to_delete: defaultdict[str, list[ImageRef]] = defaultdict(list)
        keep_ = keep or float("-inf")
        exclude_statuses_ = set(exclude_statuses or ())

        for image, tags in selected_tags.items():
            tags.sort(key=lambda item: item.created_at)
            tag_count: int = 0
            to_delete: list[ImageRef] = []
            now = datetime.now()
            for tag in tags:
                tag_count += 1
                if (
                    pattern and not pattern.match(tag.tag_name)
                ) or tag.status in exclude_statuses_:
                    continue
                too_old = False
                if grace:
                    too_old = now - tag.created_at >= grace
                if too_old and tag_count - len(to_delete) >= keep_:
                    to_delete.append(tag)
            tags_to_delete[image] = to_delete
        return tags_to_delete
