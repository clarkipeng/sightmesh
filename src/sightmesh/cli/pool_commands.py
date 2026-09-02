from __future__ import annotations

import time

from .common import *

def _pool_quota_text(usage: dict[str, Any]) -> str:
    if usage.get("metered"):
        return "metered"
    if not usage.get("known"):
        return usage.get("reason") or "quota unknown"
    resets = usage.get("resetsIn")
    suffix = (
        f", resets in {pool_core.fmt_delta(resets)}" if resets and resets > 0 else ""
    )
    return f"{usage.get('remaining'):.0f}% left{suffix}"


def _pool_row_mark(row: dict[str, Any]) -> str:
    if not row["hasCredential"]:
        return "NO CREDENTIAL"
    if row["coolingFor"]:
        return f"cooling {pool_core.fmt_delta(row['coolingFor'])}"
    if row["health"] == "unhealthy":
        return f"unhealthy: {row.get('healthReason') or 'failed'}"
    return _pool_quota_text(row["quota"]) if row["quota"] else "unprobed"


def _pool_listing_text(snapshot: dict[str, Any]) -> str:
    lines: list[str] = []
    for provider, rows in snapshot["providers"].items():
        if not rows:
            continue
        lines.append(f"\n{provider}:")
        for row in rows:
            lines.append(
                f"  {row['position']}. {row['id']:<14} {row['label']:<40} "
                f"{_pool_row_mark(row)}"
            )
    return "\n".join(lines) if lines else "pool is empty"


def _pool_read_token_input() -> str:
    """Read a token the terminal may have wrapped onto several lines.

    Each hidden read takes one line, so a two-line paste needs two reads. It
    stops as soon as the accumulated value validates, which keeps an ordinary
    single-line paste to one Enter.
    """
    print("  Paste the token (both lines if it wrapped), then Enter on a blank line.\n")
    parts: list[str] = []
    while True:
        try:
            line = getpass.getpass("token: " if not parts else "  ...: ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not line:
            break
        parts.append(line)
        token = pool_core.normalize_token("".join(parts))
        if pool_core.validate_claude_token(token) is None:
            return token
    return pool_core.normalize_token("".join(parts))


def _pool_add_claude(args: argparse.Namespace) -> int:
    pool = pool_core.load_pool()
    if pool_core.find(pool, args.name):
        raise PoolError(f"Account already exists: {args.name}")

    identity = pool_core.ambient_claude_identity()
    if not identity.get("email"):
        raise PoolError(identity.get("error", "Cannot determine the logged-in account"))

    print(
        f"\n  Currently logged in as: {identity['email']} ({identity.get('subscription')})"
    )
    print("  `claude setup-token` mints a token for THIS account.\n")
    print("  In another terminal run:  claude setup-token")
    print("  Do NOT log out first - that revokes the token.\n")
    token = _pool_read_token_input()
    problem = pool_core.validate_claude_token(token)
    if problem:
        raise PoolError(problem)

    candidate = {
        "id": args.name,
        "provider": "claude",
        "kind": "oauth",
        "label": args.label or identity["email"],
        "identity": identity,
        "token_fp": pool_core.fingerprint(token),
    }
    duplicate = pool_core.check_duplicate(pool, candidate)
    if duplicate and not args.force:
        raise PoolError(
            f"{duplicate['id']} already uses {pool_core.identity_label(duplicate)} "
            "- pass --force to add anyway"
        )

    pool_core.write_token(args.name, token)
    print("\n  validating with a real request...")
    ok, reason = pool_core.probe(candidate)
    if not ok:
        pool_core.token_path(args.name).unlink(missing_ok=True)
        raise PoolError(
            f"Token rejected: {reason}. `claude auth logout` invalidates tokens minted "
            "by that session - mint the token and add it before switching accounts."
        )

    pool.setdefault("accounts", []).append(candidate)
    pool_core.save_pool(pool)
    print(f"  added {args.name}: {pool_core.identity_label(candidate)}")
    print(f"  token stored: {pool_core.shape(token)}")
    return 0


def _pool_add_codex(args: argparse.Namespace) -> int:
    pool = pool_core.load_pool()
    if pool_core.find(pool, args.name):
        raise PoolError(f"Account already exists: {args.name}")

    codex_home = Path(os.path.expanduser(args.home or f"~/.codex-{args.name}"))
    codex_home.mkdir(parents=True, exist_ok=True)
    primary = Path.home() / ".codex" / "config.toml"
    if primary.exists() and not (codex_home / "config.toml").exists():
        shutil.copy2(primary, codex_home / "config.toml")
        print(f"  copied config.toml from ~/.codex -> {codex_home}")

    # Codex stores exactly one auth mode per CODEX_HOME, so each account owns one.
    env = {**os.environ, "CODEX_HOME": str(codex_home)}
    token = None
    if args.mode == "apikey":
        token = getpass.getpass("OpenAI API key: ").strip()
        if not token:
            raise PoolError("No key provided")
        run = subprocess.run(
            ["codex", "login", "--with-api-key"],
            env=env,
            input=token,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if run.returncode != 0:
            raise PoolError(f"codex login failed: {(run.stderr or '').strip()[:200]}")
    else:
        print(f"\n  Opening Codex browser login for '{args.name}'.")
        print("  Sign in with the ChatGPT account holding the subscription.\n")
        if subprocess.run(["codex", "login"], env=env, check=False).returncode != 0:
            raise PoolError("codex login failed")

    candidate = {
        "id": args.name,
        "provider": "codex",
        "kind": "chatgpt" if args.mode == "sub" else "apikey",
        "codex_home": str(codex_home),
        "identity": pool_core.codex_identity(str(codex_home)),
    }
    if token:
        candidate["token_fp"] = pool_core.fingerprint(token)
    candidate["label"] = args.label or pool_core.identity_label(candidate)

    duplicate = pool_core.check_duplicate(pool, candidate)
    if duplicate and not args.force:
        raise PoolError(
            f"{duplicate['id']} already uses {pool_core.identity_label(duplicate)} "
            "- pass --force to add anyway"
        )

    if token:
        pool_core.write_token(args.name, token)
    pool.setdefault("accounts", []).append(candidate)
    pool_core.save_pool(pool)
    print(f"\n  added {args.name}: {pool_core.identity_label(candidate)}")
    return 0


def _pool_verify(as_json: bool) -> int:
    """Prove every pooled account is a distinct, usable, owned account."""
    pool = pool_core.load_pool()
    accounts = pool.get("accounts", [])
    if not accounts:
        _emit("pool is empty", as_json)
        return 0

    changed = False
    for account in accounts:
        if account.get("provider") == "codex":
            fresh = pool_core.codex_identity(
                os.path.expanduser(account.get("codex_home", ""))
            )
            if fresh and fresh != account.get("identity"):
                account["identity"] = fresh
                changed = True
    if changed:
        pool_core.save_pool(pool)

    seen: dict[str, str] = {}
    results = []
    for account in accounts:
        key = pool_core.identity_key(account)
        duplicate_of = seen.get(key) if key else None
        if key and not duplicate_of:
            seen[key] = account["id"]
        usage = pool_core.quota_cached(account, force=True)
        ok, reason = pool_core.probe(account)
        results.append(
            {
                "id": account["id"],
                "identity": pool_core.identity_label(account),
                "unique": "duplicate" if duplicate_of else "yes" if key else "unknown",
                "duplicate_of": duplicate_of,
                "quota": _pool_quota_text(usage),
                "health": "ok" if ok else reason,
            }
        )

    problems = [
        r
        for r in results
        if r["duplicate_of"] or r["health"] not in ("ok", "usage limit")
    ]
    if as_json:
        _emit({"accounts": results, "problems": [r["id"] for r in problems]}, True)
        return 1 if problems else 0

    print(f"\n{'account':<14} {'identity':<42} {'unique':<10} {'quota':<28} health")
    print("-" * 116)
    for row in results:
        print(
            f"{row['id']:<14} {row['identity']:<42} {row['unique']:<10} "
            f"{row['quota']:<28} {row['health']}"
        )
    print()
    for row in problems:
        if row["duplicate_of"]:
            print(
                f"  ! {row['id']} is the same account as {row['duplicate_of']} - remove one"
            )
        else:
            print(f"  ! {row['id']} is not usable: {row['health']}")
    if not problems:
        print("  all accounts distinct and usable")
    print()
    return 1 if problems else 0


def cmd_pool(args: argparse.Namespace) -> int:
    action = args.pool_action

    if action == "list":
        snapshot = pool_core.snapshot()
        _emit(snapshot if args.json else _pool_listing_text(snapshot), args.json)
        return 0

    if action == "status":
        pool = pool_core.load_pool()
        report = {}
        for provider in pool_core.PROVIDERS:
            if not pool_core.accounts_for(pool, provider):
                continue
            chosen, notes = pool_core.select(provider, verify=True)
            report[provider] = {
                "selected": chosen["id"] if chosen else None,
                "skipped": notes,
            }
        if args.json:
            _emit(report, True)
            return 0
        for provider, entry in report.items():
            print(f"\n{provider}:")
            for note in entry["skipped"]:
                print(f"    {note}")
            print(f"  -> {entry['selected'] or 'NO ACCOUNT AVAILABLE'}")
        print()
        return 0

    if action == "which":
        chosen, _ = pool_core.select(args.provider, verify=args.verify)
        if not chosen:
            raise PoolError(f"No {args.provider} account available")
        _emit(chosen["id"], args.json)
        return 0

    if action == "exec":
        # Preferred launcher: the credential is handed to the child process
        # directly, so it never reaches the terminal or shell history.
        chosen, notes = pool_core.select(args.provider, verify=not args.no_verify)
        for note in notes:
            print(f"# {note}", file=sys.stderr)
        if not chosen:
            raise PoolError(f"No {args.provider} account available")
        binary = "claude" if args.provider == "claude" else "codex"
        print(
            f"# using {chosen['id']} ({pool_core.identity_label(chosen)})",
            file=sys.stderr,
        )
        overlay = {
            **os.environ,
            **pool_core.env_for(chosen),
            "SIGHTMESH_POOL_ACCOUNT": chosen["id"],
        }
        try:
            os.execvpe(binary, [binary, *args.argv], overlay)
        except FileNotFoundError as exc:
            raise PoolError(f"{binary} is not on PATH") from exc

    if action == "order":
        pool = pool_core.load_pool()
        if not args.ids:
            _emit(
                [a["id"] for a in pool_core.accounts_for(pool, args.provider)],
                args.json,
            )
            return 0
        error = pool_core.reorder(pool, args.provider, args.ids)
        if error:
            raise PoolError(error)
        pool_core.save_pool(pool)
        _emit([a["id"] for a in pool_core.accounts_for(pool, args.provider)], args.json)
        return 0

    if action == "promote":
        pool = pool_core.load_pool()
        account = pool_core.find(pool, args.name)
        if not account:
            raise PoolError(f"Unknown account: {args.name}")
        order = [a["id"] for a in pool_core.accounts_for(pool, account["provider"])]
        order.remove(args.name)
        error = pool_core.reorder(pool, account["provider"], [args.name, *order])
        if error:
            raise PoolError(error)
        pool_core.save_pool(pool)
        _emit(
            [a["id"] for a in pool_core.accounts_for(pool, account["provider"])],
            args.json,
        )
        return 0

    if action == "quota":
        pool = pool_core.load_pool()
        targets = [
            pool_core.find(pool, name)
            for name in args.names
            if pool_core.find(pool, name)
        ] or pool.get("accounts", [])
        report = [
            {
                "id": account["id"],
                "identity": pool_core.identity_label(account),
                "quota": pool_core.quota_cached(account, force=args.refresh),
            }
            for account in targets
        ]
        if args.json:
            _emit(report, True)
            return 0
        for entry in report:
            print(f"\n{entry['id']}  {entry['identity']}")
            usage = entry["quota"]
            if not usage.get("known"):
                print(f"  {usage.get('reason', 'unknown')}")
                continue
            for window in usage.get("windows", []):
                print(
                    f"  {window.get('label'):<26} {window.get('remaining')}% left"
                    f"   resets in {pool_core.fmt_delta(window.get('resetsIn') or 0)}"
                    f"  ({window.get('resetsAt')})"
                )
            print(f"  effective: {usage.get('remaining')}% remaining")
        print()
        return 0

    if action == "verify":
        return _pool_verify(args.json)

    if action == "cool":
        if not pool_core.find(pool_core.load_pool(), args.name):
            raise PoolError(f"Unknown account: {args.name}")
        seconds = pool_core.parse_duration(args.duration)
        # Cooling is monotonic, so a shorter request cannot cut a longer live
        # cooldown short. Report the deadline that actually holds rather than
        # the one that was asked for; `pool clear` is how a window is ended.
        until = pool_core.set_cooldown(args.name, seconds)
        _emit(
            f"{args.name} cooling for {pool_core.fmt_delta(until - time.time())}",
            args.json,
        )
        return 0

    if action == "clear":
        if args.all:
            pool_core.save_state({"cooldowns": {}, "probes": {}, "quota": {}})
            _emit("cleared all cooldowns", args.json)
            return 0
        if not args.name:
            raise PoolError("Provide an account id or --all")
        pool_core.clear_cooldown(args.name)
        _emit(f"cleared {args.name}", args.json)
        return 0

    if action == "remove":
        pool = pool_core.load_pool()
        if not pool_core.find(pool, args.name):
            raise PoolError(f"Unknown account: {args.name}")
        pool["accounts"] = [a for a in pool["accounts"] if a["id"] != args.name]
        pool_core.save_pool(pool)
        pool_core.token_path(args.name).unlink(missing_ok=True)
        pool_core.clear_cooldown(args.name)
        _emit(f"removed {args.name}", args.json)
        return 0

    if action == "add-claude":
        return _pool_add_claude(args)

    if action == "add-codex":
        return _pool_add_codex(args)

    if action == "serve":
        from .pool import server

        return server.serve(args.port, not args.no_open)

    raise ValueError(f"Unknown pool action: {action}")



def add_parser(sub: argparse._SubParsersAction[Any]) -> None:
    pool = sub.add_parser(
        "pool",
        help="Order accounts the operator owns and select the first with quota",
    )
    pool_sub = pool.add_subparsers(dest="pool_action", required=True)

    pool_list = pool_sub.add_parser("list", help="Pool order, identity, and quota")
    pool_list.set_defaults(func=cmd_pool)

    pool_status = pool_sub.add_parser(
        "status", help="Show which account each provider would select now"
    )
    pool_status.set_defaults(func=cmd_pool)

    pool_which = pool_sub.add_parser("which", help="Print the selected account id")
    pool_which.add_argument("provider", choices=pool_core.PROVIDERS)
    pool_which.add_argument("--verify", action="store_true")
    pool_which.set_defaults(func=cmd_pool)

    pool_exec = pool_sub.add_parser(
        "exec",
        help="Run the provider CLI on the selected account",
        description=(
            "Every argument after the provider is passed to the provider CLI, so "
            "pool options must come first: sightmesh pool exec --no-verify claude -p ok"
        ),
    )
    pool_exec.add_argument("--no-verify", action="store_true")
    pool_exec.add_argument("provider", choices=pool_core.PROVIDERS)
    pool_exec.add_argument(
        "argv", nargs=argparse.REMAINDER, help="Arguments forwarded to the provider CLI"
    )
    pool_exec.set_defaults(func=cmd_pool)

    pool_order = pool_sub.add_parser("order", help="Show or set the fallback order")
    pool_order.add_argument("provider", choices=pool_core.PROVIDERS)
    pool_order.add_argument("ids", nargs="*")
    pool_order.set_defaults(func=cmd_pool)

    pool_promote = pool_sub.add_parser("promote", help="Move an account to the front")
    pool_promote.add_argument("name")
    pool_promote.set_defaults(func=cmd_pool)

    pool_quota = pool_sub.add_parser("quota", help="Live quota windows and resets")
    pool_quota.add_argument("names", nargs="*")
    pool_quota.add_argument("--refresh", action="store_true")
    pool_quota.set_defaults(func=cmd_pool)

    pool_verify = pool_sub.add_parser(
        "verify", help="Prove every account is distinct and usable"
    )
    pool_verify.set_defaults(func=cmd_pool)

    pool_cool = pool_sub.add_parser("cool", help="Mark an account exhausted")
    pool_cool.add_argument("name")
    pool_cool.add_argument("--for", dest="duration", default="5h")
    pool_cool.set_defaults(func=cmd_pool)

    pool_clear = pool_sub.add_parser("clear", help="Clear cooldowns")
    pool_clear.add_argument("name", nargs="?")
    pool_clear.add_argument("--all", action="store_true")
    pool_clear.set_defaults(func=cmd_pool)

    pool_remove = pool_sub.add_parser(
        "remove", help="Drop an account and its stored credential"
    )
    pool_remove.add_argument("name")
    pool_remove.set_defaults(func=cmd_pool)

    pool_add_claude = pool_sub.add_parser(
        "add-claude", help="Add a Claude account from `claude setup-token`"
    )
    pool_add_claude.add_argument("name")
    pool_add_claude.add_argument("--label")
    pool_add_claude.add_argument("--force", action="store_true")
    pool_add_claude.set_defaults(func=cmd_pool)

    pool_add_codex = pool_sub.add_parser(
        "add-codex", help="Add a Codex account with its own CODEX_HOME"
    )
    pool_add_codex.add_argument("name")
    pool_add_codex.add_argument("--mode", choices=["sub", "apikey"], required=True)
    pool_add_codex.add_argument("--home", help="Exact CODEX_HOME for this account")
    pool_add_codex.add_argument("--label")
    pool_add_codex.add_argument("--force", action="store_true")
    pool_add_codex.set_defaults(func=cmd_pool)

    pool_serve = pool_sub.add_parser("serve", help="Open the local pool web UI")
    pool_serve.add_argument("--port", type=int, default=7878)
    pool_serve.add_argument("--no-open", action="store_true")
    pool_serve.set_defaults(func=cmd_pool)
