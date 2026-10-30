# File transfer

Upload files into the active repo/worktree or fetch files back into Telegram.

## Enable file transfer

=== "takopi config"

    ```sh
    takopi config set transports.telegram.files.enabled true
    takopi config set transports.telegram.files.auto_put true
    takopi config set transports.telegram.files.auto_put_mode "upload"
    takopi config set transports.telegram.files.uploads_dir "incoming"
    takopi config set transports.telegram.files.allowed_user_ids "[123456789]"
    takopi config set transports.telegram.files.deny_globs '[".git/**", ".env", ".envrc", "**/*.pem", "**/.ssh/**"]'
    ```

=== "toml"

    ```toml
    [transports.telegram.files]
    enabled = true
    auto_put = true
    auto_put_mode = "upload" # upload | prompt
    uploads_dir = "incoming"
    allowed_user_ids = [123456789]
    deny_globs = [".git/**", ".env", ".envrc", "**/*.pem", "**/.ssh/**"]
    ```

Notes:

- File transfer is **disabled by default**.
- If `allowed_user_ids` is empty, private chats are allowed and group usage requires admin privileges.

## Use an attachment as a prompt

Set `enabled = true`, `auto_put = true`, and `auto_put_mode = "prompt"` under
`[transports.telegram.files]`, then restart Takopi. Transport settings require a
restart even when config watching is enabled. The local setup script
(`scripts/configure_local.py`) enables this mode and restricts file transfer to
the configured owner.

Attach a document or photo and put your request in its caption, for example:
"Read the attached report and summarize the pending tasks."

Takopi saves the attachment under `incoming/` in the conversation's project or
worktree, then sends the caption and saved file path to that conversation's
agent. Codex, Claude and Agy use their file-reading tools to access it; the
bridge does not embed or extract the file's contents into the prompt. Supported
formats depend on the agent's tools. Photos use the largest version provided by
Telegram; send an image as a document to preserve the original file.

Albums send one task with the caption and all successfully saved file paths.
Without a caption, attachments are saved without starting an agent. Uploads are
limited to **20 MiB per file**. Existing filenames are not overwritten: rename
the attachment, or explicitly use `/file put --force incoming/filename` and then
send the task in a separate message. Files denied by path rules or user
permissions are rejected before invoking an agent.

## Upload a file (`/file put`)

Send a document with a caption:

```
/file put <path>
```

Examples:

```
/file put docs/spec.pdf
/file put /happy-gadgets @feat/camera assets/logo.png
```

If you send a file **without a caption**, Takopi saves it to `incoming/<original_filename>`.

Use `--force` to overwrite:

```
/file put --force docs/spec.pdf
```

## Fetch a file (`/file get`)

Send:

```
/file get <path>
```

Directories are zipped automatically.

## Related

- [Commands & directives](../reference/commands-and-directives.md)
- [Config reference](../reference/config.md)
