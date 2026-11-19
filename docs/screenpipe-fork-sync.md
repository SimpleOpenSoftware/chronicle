# Screenpipe fork sync

Chronicle's recorder source is the `chronicle-recorder` branch of
[`AnkushMalaker/screenpipe`](https://github.com/AnkushMalaker/screenpipe), based
directly on upstream `screenpipe/screenpipe` `main`. The historical `chronicle`
branch remains intact; the new branch does not replay its old history.

## Automated sync

`.github/workflows/sync-screenpipe-fork.yml` runs daily from Chronicle's default
branch, `dev`, and can also be started manually. It checks out `chronicle-recorder`
with full history, fetches upstream `main`, and rebases when upstream advances.
A successful rebase pushes with `--force-with-lease`; conflicts leave the fork
unchanged and open or update a Chronicle issue. This workflow checks whether the
rebase applies; it does not build, install, or validate the recorder on a device.

The workflow needs the `SCREENPIPE_FORK_TOKEN` repository secret with write
access to the fork. Start a manual dry run with:

```bash
gh workflow run sync-screenpipe-fork.yml --ref dev -F push=false
```

Use `-F` to send a boolean. The workflow also rejects the string `"false"`.

## Local sync and conflict recovery

Use a fresh directory to preserve existing work. The commands below use `origin`
for the writable fork and fetch upstream directly by URL:

```bash
git clone --branch chronicle-recorder https://github.com/AnkushMalaker/screenpipe.git screenpipe-sync
cd screenpipe-sync
expected=$(git rev-parse HEAD)
git fetch --no-tags https://github.com/screenpipe/screenpipe main
git rebase FETCH_HEAD
```

Resolve conflicts, stage only the resolved files, and run `git rebase --continue`.
Use `git rebase --abort` to abandon the attempt. Review the diff and run tests
appropriate to the affected recorder areas before publishing. A clean rebase alone
does not prove capture, privacy, or audio behavior.

```bash
git push --force-with-lease=refs/heads/chronicle-recorder:$expected origin HEAD:refs/heads/chronicle-recorder
```

The explicit expected commit protects concurrent changes to the remote branch.
Never replace it with an unconditional force push. Keep the workflow and this
runbook aligned; the recorder branch does not require a custom sync script.
