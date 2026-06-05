"""telegram_send — let an agent proactively send a Telegram message via a YATCA bot.

YATCA's normal path only *replies* to incoming messages. This tool lets any agent
(e.g. a scheduled task) *push* a message without an incoming message to reply to.

Telegram requires a real chat_id (you cannot send to a random/made-up id), but the
bot LEARNS chat ids automatically: every chat that messages the bot is recorded in
YATCA's state. So by default this tool sends to every chat the bot already knows —
no id configuration needed; just message the bot once and it can reach you.

Chat resolution: chat_id arg -> notify_chat_id (config, optional, for precision)
                 -> single allowed_chats -> all chats the bot has seen.
Bot resolution:  bot arg -> a bot flagged notify_default -> the only running bot.
"""

import asyncio
import os
from helpers.tool import Tool, Response


async def _await_on_loop(inst, coro_factory):
    """Run a bot coroutine on the bot's own event loop.

    The bot's aiohttp session is bound to the job loop the bot was started on,
    but this tool runs on the agent's loop. Awaiting the bot directly raises
    "Timeout context manager should be used inside a task". So when the bot lives
    on a different running loop, schedule the coroutine there and await its result.
    """
    loop = getattr(inst, "loop", None)
    try:
        current = asyncio.get_running_loop()
    except RuntimeError:
        current = None
    if loop is not None and loop.is_running() and loop is not current:
        fut = asyncio.run_coroutine_threadsafe(coro_factory(), loop)
        return await asyncio.wrap_future(fut)
    return await coro_factory()


def _bots_cfg():
    from helpers import plugins
    from usr.plugins.yatca.helpers.constants import PLUGIN_NAME

    cfg = plugins.get_plugin_config(PLUGIN_NAME) or {}
    return cfg.get("bots") or []


def _resolve_bot(name):
    from usr.plugins.yatca.helpers import bot_manager

    running = bot_manager.get_all_bots()  # name -> BotInstance
    if not running:
        return None, "No YATCA bot is running."
    if name:
        inst = bot_manager.get_bot(name)
        if not inst:
            return None, f"No running YATCA bot named '{name}'. Running: {', '.join(running)}."
        return inst, None
    for b in _bots_cfg():  # honor a notify_default flag if set
        if b.get("notify_default") and b.get("name") in running:
            return running[b["name"]], None
    if len(running) == 1:
        return next(iter(running.values())), None
    return None, f"Multiple bots running ({', '.join(running)}); pass bot= to choose one."


def _known_chats(bot_name):
    """Chat ids the bot has interacted with (learned from incoming messages)."""
    from helpers import files
    from usr.plugins.yatca.helpers.constants import STATE_FILE
    import json

    try:
        p = files.get_abs_path(STATE_FILE)
        state = json.loads(files.read_file(p)) if os.path.isfile(p) else {}
    except Exception:
        state = {}
    # Learned chats live under state["chats"], keyed "<bot>:<user_id>:<chat_id>"
    # (see handler._map_key). Read that nested dict, not the top-level keys.
    chats = state.get("chats", {}) if isinstance(state, dict) else {}
    out, seen = [], set()
    prefix = f"{bot_name}:"
    for k in chats:
        if isinstance(k, str) and k.startswith(prefix):
            parts = k.split(":")
            if len(parts) >= 3 and parts[2] and parts[2] not in seen:
                seen.add(parts[2])
                out.append(parts[2])
    return out


def _resolve_targets(inst, chat_id):
    if chat_id:
        return [chat_id], None
    bcfg = next((b for b in _bots_cfg() if b.get("name") == inst.name), {})
    if bcfg.get("notify_chat_id"):
        return [bcfg["notify_chat_id"]], None
    allowed = bcfg.get("allowed_chats") or []
    if len(allowed) == 1:
        return [allowed[0]], None
    known = _known_chats(inst.name)
    if known:
        return known, None
    return None, (
        f"The bot '{inst.name}' has not been messaged yet, so it has no chat to send to. "
        "Send any message (e.g. /start) to the bot in Telegram once — it will remember your "
        "chat and future sends will reach you automatically. (Or set notify_chat_id / pass chat_id.)"
    )


class TelegramSend(Tool):

    async def execute(self, **kwargs):
        from usr.plugins.yatca.helpers.dependencies import ensure_dependencies
        from usr.plugins.yatca.helpers import telegram_client

        ensure_dependencies()

        message = (self.args.get("message") or "").strip()
        if not message:
            return Response(message="telegram_send: 'message' is required.", break_loop=False)
        title = (self.args.get("title") or "").strip()

        inst, err = _resolve_bot((self.args.get("bot") or "").strip() or None)
        if err:
            return Response(message=f"telegram_send: {err}", break_loop=False)

        targets, err = _resolve_targets(inst, (self.args.get("chat_id") or "").strip() or None)
        if err:
            return Response(message=f"telegram_send: {err}", break_loop=False)

        body = f"*{title}*\n\n{message}" if title else message
        try:
            html = telegram_client.md_to_telegram_html(body)
        except Exception:
            html = body

        sent, failed = [], []
        for raw in targets:
            try:
                cid = int(str(raw).strip())
            except (TypeError, ValueError):
                failed.append(f"{raw} (invalid id)")
                continue
            try:
                msg_id = await _await_on_loop(
                    inst,
                    lambda c=cid: telegram_client.send_text(inst.bot, c, html, parse_mode="HTML"),
                )
                (sent if msg_id is not None else failed).append(str(cid))
            except Exception as e:
                failed.append(f"{cid} ({e})")

        if sent and not failed:
            return Response(message=f"Telegram sent to {len(sent)} chat(s) via '{inst.name}': {', '.join(sent)}.", break_loop=False)
        if sent and failed:
            return Response(message=f"Telegram sent to {', '.join(sent)}; failed: {', '.join(failed)}.", break_loop=False)
        return Response(message=f"telegram_send: all sends failed: {', '.join(failed) or '(none)'}.", break_loop=False)
