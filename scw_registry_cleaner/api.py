import logging
import platform
import pprint
import sys
import time
from logging import NullHandler
from typing import Optional

import requests
from requests.adapters import HTTPAdapter
from requests.exceptions import HTTPError

import scw_registry_cleaner

# Prevent message "No handlers could be found for logger "scaleway"" to be
# displayed.
logger = logging.getLogger(__name__)
logger.addHandler(NullHandler())

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


class CustomAdapter(HTTPAdapter):
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

    def send(
        self, request, stream=False, timeout=None, verify=True, cert=None, proxies=None
    ) -> requests.Response:
        """Makes a request to the Scaleway API, and wait patiently if there is
        an ongoing maintenance.
        """
        retry = 0

        while True:
            try:
                resp = super().send(request, stream, timeout, verify, cert, proxies)
                if self.logging:
                    pprint.pprint(resp.json())
                return resp
            except HTTPError as exc:
                # Not a maintenance exception
                if exc.response.status_code not in (502, 503, 504):
                    raise

                retry += 1
                retry_in = self.retry_in(retry)

                if retry >= self.MAX_RETRIES:
                    logger.error(
                        f"API endpoint still in maintenance after {self.MAX_RETRIES} attempts. "
                        f"Stop trying."
                    )
                    raise

                logger.info(
                    f"API endpoint is currently in maintenance. Try again in "
                    f"{retry_in} seconds... (retry {retry} on {self.max_retries})"
                )
                time.sleep(retry_in)


class RegistryAPI:
    """The default region is par1 as it was the first availability zone
    provided by Scaleway, but it could change in the future.
    """

    base_url = None
    user_agent = "scw-sdk/%s Python/%s %s" % (
        scw_registry_cleaner.__version__,
        " ".join(sys.version.split()),
        platform.platform(),
    )

    def __init__(
        self,
        auth_token: Optional[str] = None,
        auth_jwt: Optional[str] = None,
        user_agent: Optional[str] = None,
        verify_ssl: bool = True,
        region: Optional[str] = None,
        base_url: Optional[str] = None,
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

        self.base_url = REGIONS.get(self.region)["url"]
        self.session = self.make_requests_session(debug)

    def make_requests_session(self, logging: bool):
        """Attaches headers needed to query Scaleway APIs."""
        session = requests.Session()

        session.headers.update({"User-Agent": self.user_agent})

        if self.auth_token:
            # HTTP headers must always be ISO-8859-1 encoded
            session.headers.update({"X-Auth-Token": self.auth_token.encode("latin1")})
        if self.auth_jwt:
            session.headers.update({"X-Session-Token": self.auth_jwt.encode("latin1")})

        session.verify = self.verify_ssl
        session.mount("https://", CustomAdapter(debug=logging))

        return session

    def get_namespace(self, name: Optional[str] = None) -> dict:
        resp = self.session.get(f"{self.base_url}/namespaces", params={"name": name})
        return resp.json()["namespaces"]

    def get_images(self, namespace_id: str, name: Optional[str] = None) -> dict:
        params = {"namespace_id": namespace_id}
        if name:
            params["name"] = name
        resp = self.session.get(f"{self.base_url}/images", params=params)
        return resp.json()["images"]

    def get_image_tags(self, image_id: str) -> dict:
        resp = self.session.get(f"{self.base_url}/images/{image_id}/tags")
        return resp.json()["tags"]

    def delete_tag(self, id: str) -> dict:
        resp = self.session.delete(f"{self.base_url}/tags/{id}")
        return resp.json()
