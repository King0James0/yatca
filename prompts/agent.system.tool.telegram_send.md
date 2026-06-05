### telegram_send
proactively send a Telegram message through a YATCA bot (does not end the task)
use to push a notification/summary to the user on Telegram when there is no incoming
message to reply to — e.g. from a scheduled task or background job
args: `message` (required), optional `title`, `bot`, `chat_id`
`message` is Markdown — it is rendered to Telegram HTML for you
`bot`: which YATCA bot to send via, by its configured name; omit to use the default/only bot
`chat_id`: destination chat; omit to auto-send to every chat the bot has talked to (it learns chat ids from incoming messages — no id setup needed), or set notify_chat_id in config for a fixed target
returns a confirmation, or a clear error only if no bot is running or the bot has never been messaged
usage:
~~~json
{
  "thoughts": ["The watch found new commits; notify on Telegram."],
  "tool_name": "telegram_send",
  "tool_args": { "title": "GitHub watch", "message": "2 new across 6 repos…", "bot": "a0_agent0_bot" }
}
~~~
