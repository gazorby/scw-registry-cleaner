import argparse
import datetime as dt
import logging
import os
import re
from collections import defaultdict, namedtuple
from typing import Dict, List

from scw_registry_cleaner.api import RegistryAPI

logger = logging.getLogger(__name__)

parser = argparse.ArgumentParser(description="Delete old tags from scaleway registry")

parser.add_argument(
    "-n",
    "--namespace",
    metavar="NAMESPACE",
    action="append",
    nargs="+",
    default=[],
    required=True,
)
parser.add_argument(
    "-k",
    "--keep",
    metavar="NUMBER",
    type=int,
    nargs=1,
    help="Minimum tags to keep outside of the grace duration",
    default=None
)
parser.add_argument(
    "-g",
    "--grace",
    metavar="DURATION",
    nargs=1,

    help="Delete any selected tags older than the specified duration (in hours, minutes or seconds). Valid examples: '48hr', '3600s', '24hr30m'",
)
parser.add_argument(
    "-p",
    "--pattern",
    metavar="REGEX",
    nargs=1,
    default=None,
    help="Filter tags for that can be selected for deletion. Any tag not matching the pattern will not be deleted",
)
parser.add_argument(
    "--dry-run",
    action="store_true",
    help="Do not delete anything and print image names that would be deleted",
)
parser.add_argument(
    "--debug", action="store_true", help="Print responses from scaleway registry API"
)

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

    if args.keep is None:
        keep = float("-inf")
    else:
        keep = args.keep[0]

    if grace:
        parts = re.match(
            r"((?P<hours>\d+?)hr)?((?P<minutes>\d+?)m)?((?P<seconds>\d+?)s)?",
            args.grace[0],
        )
        parts = parts.groupdict()
        time_params = {}
        for name, param in parts.items():
            if param:
                time_params[name] = int(param)
        grace = dt.timedelta(**time_params)

    if pattern is not None:
        pattern = re.compile(pattern[0])

    api = RegistryAPI(auth_token=api_token, debug=args.debug)

    selected_tags: Dict[str, List[Tag]] = defaultdict(list)
    tags_to_delete: Dict[str, List[Tag]] = defaultdict(list)

    for n in namespaces:
        resp = api.get_namespace(name=n)
        namespace_id = resp[0]["id"]
        images = api.get_images(namespace_id)
        for image in images:
            tags = api.get_image_tags(image["id"])
            for t in tags:
                created_at_dt = dt.datetime.fromisoformat(
                    t["created_at"].replace("Z", "")
                )
                if pattern is None or pattern.match(t["name"]):
                    tag = Tag(
                        id=t["id"],
                        name=t["name"],
                        created_at=created_at_dt,
                        full_name=f"{n}/{image['name']}:{t['name']}",
                    )
                    selected_tags[image["name"]].append(tag)

    for image, tags in selected_tags.items():
        tags.sort(key=lambda item: item.created_at)
        to_delete, old_tags = [], []
        now = dt.datetime.now()
        for t in tags:
            too_old = False
            if grace:
                too_old = now - t.created_at >= grace
            if too_old:
                old_tags.append(t)
            if too_old and len(old_tags) - len(to_delete) >= keep:
                to_delete.append(t)
        tags_to_delete[image] = to_delete


    if dry_run:
        print("\nTags to delete:\n")

    for name, tags in tags_to_delete.items():
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
