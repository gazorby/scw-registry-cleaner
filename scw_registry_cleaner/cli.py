import argparse
import datetime as dt
import logging
import os
import re
from collections import defaultdict, namedtuple
from typing import List

from scw_registry_cleaner.api import RegistryAPI

logger = logging.getLogger(__name__)

parser = argparse.ArgumentParser(description="Delete old images from scaleway registry")

parser.add_argument(
    "-n",
    "--namespace",
    metavar="NAMESPACE",
    action="append",
    nargs="+",
    default=[],
    required=True,
)
parser.add_argument("-k", "--keep", metavar="NUMBER", type=int, nargs=1)
parser.add_argument("-g", "--grace", metavar="DURATION", nargs=1)
parser.add_argument("-p", "--pattern", metavar="REGEX", nargs=1, default=None)
parser.add_argument("--dry-run", action="store_true")

Tag = namedtuple("Tag", ["id", "name", "created_at", "full_name"])


if __name__ == "__main__":
    api_token = os.getenv("SCW_SECRET_KEY")
    region = os.getenv("SCW_REGION", None)

    # Parse args

    args = parser.parse_args()
    namespaces: List[str] = args.namespace[0]
    keep = args.keep
    grace = args.grace
    pattern = args.pattern
    dry_run: bool = args.dry_run

    if keep is None:
        keep = float("-inf")

    if grace:
        parts = re.match(r'((?P<hours>\d+?)hr)?((?P<minutes>\d+?)m)?((?P<seconds>\d+?)s)?', args.grace[0])
        parts = parts.groupdict()
        time_params = {}
        for name, param in parts.items():
            if param:
                time_params[name] = int(param)
        grace = dt.timedelta(**time_params)


    if args.pattern is not None:
        pattern = re.compile(args.pattern[0])

    api = RegistryAPI(auth_token=api_token)

    selected_tags = defaultdict(list)

    for n in namespaces:
        resp = api.get_namespace(name=n)
        namespace_id = resp[0]["id"]
        images = api.get_images(namespace_id)
        for image in images:
            tags = api.get_image_tags(image["id"])
            for t in tags:
                created_at_dt = dt.datetime.fromisoformat(t["created_at"].replace("Z", ""))
                if pattern is None or pattern.match(t["name"]):
                    tag = Tag(id=t["id"], name=t["name"], created_at=created_at_dt, full_name=f"{n}/{image['name']}:{t['name']}")
                    selected_tags[image["name"]].append(tag)

    for image, tags in selected_tags.items():
        tags.sort(key=lambda item: item.created_at)

        if len(tags) <= keep:
            continue
        if grace:
            now = dt.datetime.now()
            tags = [t for t in tags if now - t.created_at >= grace ]
            selected_tags[image] = tags

    if dry_run:
        print("\nTags to delete:\n")

    for name, tags in selected_tags.items():
        if not tags:
            continue
        if dry_run:
            print(f"- {name}:\n")
            print("\n".join(f"  {t.full_name}" for t in tags))
            print()
        else:
            for t in tags:
                api.delete_tag(t.id)
                print(f"Deleted {t.full_name}")
