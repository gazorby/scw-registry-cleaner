#! /sur/bin/env python

"""CLI interface."""

from __future__ import annotations

import argparse
import logging
import os
import re
from datetime import timedelta
from typing import TYPE_CHECKING

import anyio
from anyio import create_task_group
from prettytable import PrettyTable

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
    "--region",
    metavar="REGION",
    nargs=1,
    help="Scaleway registry region",
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
    """Delete namespace tag older than the specified age.

    Args:
        api: A `RegistryAPI` instance
        namespace: Name of the namespace in which to get tags
        grace: Minimal age tag age. Defaults to None.
        keep: Minimal number of tags to keep per image. Defaults to None.
        pattern: Regex pattern that must match tag names. Defaults to None.
        dry_run: If True, print tags that would be deleted, but don't delete them.
            Defaults to False.
    """
    tags_to_delete = await api.get_old_tags(
        namespace=namespace,
        grace=grace,
        keep=keep,
        pattern=pattern,
        exclude_statuses=[TagStatus.DELETING],
    )

    table = PrettyTable(field_names=["Age", "Image ref"], align="l")
    tag_ids: list[str] = []

    for name, tags in tags_to_delete.items():
        table.add_row([f"{name} image", ""], divider=True)

        for i, tag in enumerate(tags):
            tag_ids.append(tag.tag_id)
            days, hours = tag.age()
            divider = i == len(tags) - 1
            table.add_row([f"{days} days, {hours} hours", tag.ref], divider=divider)

    if not dry_run:
        await api.bulk_delete_tag(tag_ids)

    title = "Tags that would be deleted" if dry_run else "Deleted tags"
    print(f"\n{title}:\n")
    print(f"{table}\n")


async def delete_old_namesapces_tags(
    token: str,
    namespaces: list[str],
    grace: timedelta | None,
    region: str | None = None,
    keep: int | None = None,
    pattern: Pattern | None = None,
    dry_run: bool = False,
    debug: bool = False,
) -> None:
    """Delete tags in the specified namespaces
        older than the specified age

    Args:
        api: A `RegistryAPI` instance
        namespaces: Namespace names in which to delete tags from
        grace: Minimal age tag age. Defaults to None.
        keep: Minimal number of tags to keep per image. Defaults to None.
        pattern: Regex pattern that must match tag names. Defaults to None.
        dry_run: If True, print tags that would be deleted, but don't delete them.
            Defaults to False.
    """
    async with RegistryAPI(token=token, debug=debug, region=region) as api:
        async with create_task_group() as task_group:
            for namespace in namespaces:
                task_group.start_soon(
                    delete_old_tags, api, namespace, grace, keep, pattern, dry_run
                )


def main() -> None:
    """CLI entrypoint."""
    api_token: str | None = os.getenv("SCW_SECRET_KEY")
    region: str | None = os.getenv("SCW_REGION")

    # Parse args
    args = parser.parse_args()
    namespaces: list[str] = args.namespace[0]
    keep_arg: list[int] | None = args.keep
    grace_arg: list[str] = args.grace
    pattern_arg: list[str] | None = args.pattern
    dry_run: bool = args.dry_run

    if args.scw_secret_key is not None:
        api_token = args.scw_secret_key[0]
    if args.region is not None:
        region = args.region[0]

    keep = float("-inf") if keep_arg is None else keep_arg[0]
    grace, pattern = None, None

    if grace_arg:
        match = re.match(TIMEDELTA_REGEX, grace_arg[0])
        if not match:
            raise ValueError(f"Invalid duration expression {grace_arg[0]}")
        groups = match.groupdict()
        time_params = {name: int(param) for name, param in groups.items() if param}
        grace = timedelta(**time_params)

    if pattern_arg is not None:
        pattern = re.compile(pattern_arg[0])

    anyio.run(
        delete_old_namesapces_tags,
        api_token,
        namespaces,
        grace,
        region,
        keep,
        pattern,
        dry_run,
        args.debug,
    )


if __name__ == "__main__":
    main()
