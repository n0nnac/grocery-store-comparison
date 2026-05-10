#!/usr/bin/env python3
"""End-to-end weekly meal planning entrypoint.

Wraps the refresh / context / pricing pipeline into a single command, so
the workflow from this Claude session looks like:

    python3 plan.py week               # refresh data + emit planning context
    # ... read the context, write a plan JSON ...
    python3 plan.py price plan.json    # price the plan, return a shopping list

Subcommands:

- refresh   Refresh stale base prices and pull this week's deals
            (Safeway + Giant).
- context   Emit the cross-store planning JSON context (this week's
            deals + base prices for both stores).
- week      Refresh + context in one go. The default planning entrypoint.
- price     Price a meal-plan JSON returned from a planning pass.

The "planning" step is intentionally not in this file. The intent is for
a human or an LLM to read the context, then return a plan JSON shaped per
`meal_price_tool.py estimate-plan`'s contract. `plan.py price` round-trips
that plan into a priced shopping list with a cross-store breakdown.
"""

from __future__ import annotations

import argparse
import shlex
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent
SAFEWAY_REFRESH = ROOT / "safeway_refresh_prices.py"
SAFEWAY_DEAL_ENRICH = ROOT / "safeway_weekly_deal_enrich.py"
GIANT_REFRESH = ROOT / "giant_refresh_prices.py"
GIANT_FLIPP = ROOT / "giant_flipp_deals.py"
SAFEWAY_INSPIRATION = ROOT / "safeway_meal_inspiration.py"
MEAL_PRICE_TOOL = ROOT / "meal_price_tool.py"


def run(cmd, label=None, allow_fail=False, stream=True):
    if label:
        print(f"\n--- {label} ---", file=sys.stderr)
    print("$ " + " ".join(shlex.quote(str(c)) for c in cmd), file=sys.stderr)
    result = subprocess.run([str(c) for c in cmd])
    if result.returncode != 0 and not allow_fail:
        sys.exit(result.returncode)
    return result


def cmd_refresh(args):
    if not args.no_safeway:
        safeway_cmd = ["python3", SAFEWAY_REFRESH]
        if args.fill_only:
            safeway_cmd.append("--fill-missing-only")
        if args.stale_days:
            safeway_cmd.extend(["--stale-days", str(args.stale_days)])
        run(safeway_cmd, label="Safeway base prices")

        # Auto-enrich the active weekly deals JSON with structured promo
        # metadata and per-deal pack mechanic. Both subcommands are
        # idempotent (backfill-promos skips deals that already have a
        # populated `promotion` field; verify-packs skips deals that
        # already have a `verified_on` timestamp), so calling them every
        # refresh is cheap when the deals file is unchanged and does real
        # work only when a fresh weekly ad has been imported.
        if not args.skip_deal_enrichment:
            run(["python3", SAFEWAY_DEAL_ENRICH, "--backfill-promos", "--write"],
                label="Safeway deal promo metadata", allow_fail=True)
            run(["python3", SAFEWAY_DEAL_ENRICH, "--verify-packs", "--write"],
                label="Safeway deal pack-mechanic verification", allow_fail=True)

    if not args.no_giant:
        giant_cmd = ["python3", GIANT_REFRESH]
        if args.fill_only:
            giant_cmd.append("--fill-missing-only")
        if args.stale_days:
            giant_cmd.extend(["--stale-days", str(args.stale_days)])
        run(giant_cmd, label="Giant base prices", allow_fail=True)
        run(["python3", GIANT_FLIPP, "fetch", "--write"],
            label="Giant Flipp circular", allow_fail=True)
    print("\nRefresh complete.", file=sys.stderr)


def cmd_context(args):
    ctx_args = ["python3", SAFEWAY_INSPIRATION, "context"]
    if args.no_giant_deals:
        ctx_args.append("--no-giant-deals")
    if args.limit is not None:
        ctx_args.extend(["--limit", str(args.limit)])
    if getattr(args, "ideas", None) is not None:
        ctx_args.extend(["--ideas", str(args.ideas)])
    if args.write:
        ctx_args.append("--write")
        if args.output:
            ctx_args.extend(["--output", args.output])
    run(ctx_args)


def cmd_week(args):
    """Refresh data, then emit the planning context."""
    if not args.no_refresh:
        cmd_refresh(args)
    cmd_context(args)
    print(
        "\nNext step: read the context above (or `--write --output plan_context.json`) "
        "and produce a plan JSON shaped per the pricing_return_contract.\n"
        "Then: python3 plan.py price <plan.json>",
        file=sys.stderr,
    )


def cmd_price(args):
    price_args = ["python3", MEAL_PRICE_TOOL, "estimate-plan", args.plan_file]
    if args.no_resolve_missing:
        price_args.append("--no-resolve-missing")
    if args.write_resolved:
        price_args.append("--write-resolved")
    if args.no_coupons:
        price_args.append("--no-coupons")
    if args.deals_file:
        price_args.extend(["--deals-file", args.deals_file])
    if args.json:
        price_args.append("--json")
    run(price_args)


def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_refresh = sub.add_parser(
        "refresh",
        help="Refresh stale base prices and pull this week's deals (Safeway + Giant).",
    )
    p_refresh.add_argument("--no-safeway", action="store_true", help="Skip the Safeway refresh")
    p_refresh.add_argument("--no-giant", action="store_true", help="Skip the Giant refresh (browser session not required)")
    p_refresh.add_argument("--stale-days", type=int, default=7, help="Refresh saved prices older than this many days")
    p_refresh.add_argument("--fill-only", action="store_true", help="Only fill ingredients without saved prices; preserve curated bases")
    p_refresh.add_argument("--skip-deal-enrichment", action="store_true", help="Skip the auto promo-backfill + pack-verify steps that follow the Safeway base-price refresh")
    p_refresh.set_defaults(func=cmd_refresh)

    p_context = sub.add_parser(
        "context",
        help="Emit the cross-store planning JSON context.",
    )
    p_context.add_argument("--no-giant-deals", action="store_true", help="Skip the Giant Flipp circular section")
    p_context.add_argument("--limit", type=int, help="Number of ranked deals to include")
    p_context.add_argument("--ideas", type=int, help="Number of meal-idea seeds to include")
    p_context.add_argument("--write", action="store_true", help="Write context JSON to disk instead of printing")
    p_context.add_argument("--output", help="Output path when --write is set")
    p_context.set_defaults(func=cmd_context)

    p_week = sub.add_parser(
        "week",
        help="Refresh data + emit planning context. The default end-to-end pre-plan command.",
    )
    p_week.add_argument("--no-refresh", action="store_true", help="Skip the refresh step (use existing cached data)")
    p_week.add_argument("--no-safeway", action="store_true")
    p_week.add_argument("--no-giant", action="store_true")
    p_week.add_argument("--no-giant-deals", action="store_true")
    p_week.add_argument("--stale-days", type=int, default=7)
    p_week.add_argument("--fill-only", action="store_true")
    p_week.add_argument("--skip-deal-enrichment", action="store_true", help="Skip the promo-backfill + pack-verify enrichment that follows the Safeway base-price refresh")
    p_week.add_argument("--limit", type=int)
    p_week.add_argument("--ideas", type=int)
    p_week.add_argument("--write", action="store_true")
    p_week.add_argument("--output")
    p_week.set_defaults(func=cmd_week)

    p_price = sub.add_parser(
        "price",
        help="Price a meal-plan JSON returned by an LLM (alias for meal_price_tool estimate-plan).",
    )
    p_price.add_argument("plan_file", help="JSON file, or Markdown file containing a JSON block")
    p_price.add_argument("--no-resolve-missing", action="store_true", help="Skip live Safeway lookups for unknown ingredients")
    p_price.add_argument("--no-coupons", action="store_true")
    p_price.add_argument("--deals-file")
    p_price.add_argument("--write-resolved", action="store_true", help="Persist newly-resolved ingredient prices to meal_prices.json")
    p_price.add_argument("--json", action="store_true", help="Print priced plan JSON")
    p_price.set_defaults(func=cmd_price)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
