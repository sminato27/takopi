# Projects

Projects let you route messages to repos from anywhere using `/alias`.

## Create or remove a project from Telegram

Users listed in `transports.telegram.allowed_user_ids` can manage projects:

```text
/project_new my_app
/project_new my_app "/absolute/path/my app"
/project_delete
/project_delete my_app
```

`/project_new NAME [PATH]` creates the directory and registers it. Without a
path, it uses `~/projects/NAME` on the machine running the bot. An existing
directory is registered without changing its files. Paths must be absolute or
start with `~/`; quote paths containing spaces. New names use 1–32 lowercase
letters, digits or underscores and cannot match a command or engine name.
Existing project names cannot be overwritten. This creates a directory, without
initializing Git or generating application code.

`/project_delete` offers project buttons; `/project_delete NAME` goes directly
to the selected project. The bot shows its absolute directory and asks:

- **Excluir apenas do Takopi/bot** removes the registration and preserves files.
- **Excluir do Takopi/bot e arquivos locais** opens a second confirmation with
  the exact directory. Confirming permanently deletes that directory and all
  its contents, including hidden files and any worktrees inside it. Worktrees
  outside the directory are left in place.
- **Cancelar** leaves the registration and files unchanged.

Buttons expire after ten minutes, can be used only once, and belong to the
requesting user and conversation. Restarting the bot invalidates them. A changed
directory or registration requires a new request. The text fallback
`/project_delete NAME confirmar` removes **only the registration**.

Both removal modes preserve Telegram topics and the bot's session state and
remove a matching `default_project`. Bound conversations cannot start further
tasks until the project is registered again. Removing only the registration
does not cancel existing tasks. To restore access to preserved files, register
the same name and original directory again.

Deleting files is refused while the bot has active or queued work. Protected
directories (including home, the bot and its configuration), symlinked project
paths, mount points, and directories overlapping another registered project
or its worktrees cannot be deleted. Symlinks inside a project are removed
without deleting their targets. Stop external tools using the directory before
confirming; the bot cannot coordinate with unrelated processes.

The directory is temporarily moved to a `.takopi-delete-*` sibling before
saving the configuration. A save failure restores it without deleting files.
If file deletion fails after the registration is removed, the bot reports the
remaining directory for manual recovery or cleanup. If the process stops
during removal, inspect these sibling directories before retrying; deletion
may have been partial. A failure to restore the original location also reports
the recovery path. No automatic deletion retry runs after a restart.

Both commands save the configuration and update `/projects`, the `/new`
selector and the Telegram menu immediately, even with `watch_config = false`.
They do not start an agent. Without an explicit user allowlist, project
management is unavailable. Config changes use the same atomic writer as the
CLI; avoid editing the config externally at the same time.

## Register a repo as a project

```sh
cd ~/dev/happy-gadgets
takopi init happy-gadgets
```

This adds a project to your config:

=== "takopi config"

    ```sh
    takopi config set projects.happy-gadgets.path "~/dev/happy-gadgets"
    ```

=== "toml"

    ```toml
    [projects.happy-gadgets]
    path = "~/dev/happy-gadgets"
    ```

## Target a project from chat

Send:

```
/happy-gadgets pinky-link two threads
```

## Project-specific settings

Projects can override global defaults:

=== "takopi config"

    ```sh
    takopi config set projects.happy-gadgets.path "~/dev/happy-gadgets"
    takopi config set projects.happy-gadgets.default_engine "claude"
    takopi config set projects.happy-gadgets.worktrees_dir ".worktrees"
    takopi config set projects.happy-gadgets.worktree_base "master"
    ```

=== "toml"

    ```toml
    [projects.happy-gadgets]
    path = "~/dev/happy-gadgets"
    default_engine = "claude"
    worktrees_dir = ".worktrees"
    worktree_base = "master"
    ```

If you expect to edit config while Takopi is running, enable hot reload:

=== "takopi config"

    ```sh
    takopi config set watch_config true
    ```

=== "toml"

    ```toml
    watch_config = true
    ```

## Set a default project

If you mostly work in one repo:

=== "takopi config"

    ```sh
    takopi config set default_project "happy-gadgets"
    ```

=== "toml"

    ```toml
    default_project = "happy-gadgets"
    ```

## Related

- [Context resolution](../reference/context-resolution.md)
- [Worktrees](worktrees.md)
