#! /sur/bin/env python

from __future__ import annotations

import argparse
import logging
import os
import re
from datetime import timedelta
from typing import TYPE_CHECKING

import anyio
from anyio import create_task_group

from scw_registry_cleaner.api import RegistryAPI, TagStatus

if TYPE_CHECKING:
    from re import Pattern


logger = logging.getLogger(__name__)

parser = argparse.ArgumentParser(description="Delete old tags from scaleway registry")

parser.add_argument(
    "--scw-secret-key",
    metavar="SECRET",
    nargs=1,
    help="Scaleway secret key to authenticate the registry",
    default=None,
)
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
    default=None,
)
parser.add_argument(
    "-g",
    "--grace",
    metavar="DURATION",
    nargs=1,
    help=(
        "Delete any selected tags older than the specified duration"
        " (in hours, minutes or seconds)."
        " Valid examples: '48hr', '3600s', '24hr30m'"
    ),
)
parser.add_argument(
    "-p",
    "--pattern",
    metavar="REGEX",
    nargs=1,
    default=None,
    help=(
        "Filter tags for that can be selected for deletion."
        " Any tag not matching the pattern will not be deleted"
    ),
)
parser.add_argument(
    "--dry-run",
    action="store_true",
    help="Do not delete anything and print image names that would be deleted",
)
parser.add_argument(
    "--debug", action="store_true", help="Print responses from scaleway registry API"
)

TIMEDELTA_REGEX = r"((?P<hours>\d+?)hr)?((?P<minutes>\d+?)m)?((?P<seconds>\d+?)s)?"


async def delete_old_tags(
    api: RegistryAPI,
    namespace: str,
    grace: timedelta | None,
    keep: int | None = None,
    pattern: Pattern | None = None,
    dry_run: bool = False,
) -> None:
    tags_to_delete = await api.get_old_tags(namespace, grace, keep, pattern)

    if dry_run:
        print("\nTags to delete:\n")

    for name, tags in tags_to_delete.items():
        if not tags:
            continue
        if dry_run:
            print(f"- {name}:\n")
            print("\n".join(f"\t{tag}" for tag in tags) + "\n")
        else:
            await api.bulk_delete_tag(
                [tag.id for tag in tags if tag.status is not TagStatus.DELETING]
            )


async def delete_old_namesapces_tags(
    api: RegistryAPI,
    namespaces: list[str],
    grace: timedelta | None,
    keep: int | None = None,
    pattern: Pattern | None = None,
    dry_run: bool = False,
) -> None:
    async with create_task_group() as tg:
        for namespace in namespaces:
            tg.start_soon(
                delete_old_tags, api, namespace, grace, keep, pattern, dry_run
            )


if __name__ == "__main__":
    api_token = os.getenv("SCW_SECRET_KEY")
    region = os.getenv("SCW_REGION", None)

    # Parse args
    args = parser.parse_args()
    namespaces: list[str] = args.namespace[0]
    keep_arg: list[int] | None = args.keep
    grace_arg: list[str] = args.grace
    pattern_arg: list[str] | None = args.pattern
    dry_run: bool = args.dry_run

    if args.scw_secret_key is not None:
        api_token = args.scw_secret_key[0]

    keep = float("-inf") if keep_arg is None else keep_arg[0]
    grace, pattern = None, None

    if grace_arg:
        match = re.match(TIMEDELTA_REGEX, grace_arg[0])
        if not match:
            raise ValueError(f"Invalid duration expression {grace_arg[0]}")
        match = match.groupdict()
        time_params = {name: int(param) for name, param in match.items() if param}
        grace = timedelta(**time_params)

    if pattern_arg is not None:
        pattern = re.compile(pattern_arg[0])

    api = RegistryAPI(auth_token=api_token, debug=args.debug)

    anyio.run(
        delete_old_namesapces_tags, api, namespaces, grace, keep, pattern, dry_run
    )
