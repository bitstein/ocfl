"""Next-step hints: each one a full `ocfl` command the operator can copy.

Global options (`-r`, `--staging-root`) are left out: the hint runs in the
same environment as the command that printed it.
"""

import shlex

from ocfl_cli.model.metadata import NextActionKind


def stage(verb: str, *args: str) -> str:
    """`ocfl stage VERB ARGS...`, each argument shell-quoted."""
    return " ".join(["ocfl stage", verb, *(shlex.quote(a) for a in args)])


def batch(verb: str, batch_id: str, *flags: str) -> str:
    """`ocfl stage batch VERB BATCH FLAGS...`."""
    return stage("batch", verb, batch_id, *flags)


def next_command(kind: NextActionKind, object_id: str) -> str:
    """The command that takes a Staging Area to its next step ("" for none)."""
    match kind:
        case NextActionKind.ADD_CONTENT:
            return f"{stage('add', object_id)} PATH"
        case NextActionKind.TIMESTAMP_CONTENT | NextActionKind.TIMESTAMP_INVENTORY:
            return stage("timestamp", object_id)
        case NextActionKind.UPGRADE_CONTENT | NextActionKind.UPGRADE_INVENTORY:
            return stage("upgrade", object_id, "--watch")
        case NextActionKind.RETRY_CONTENT | NextActionKind.RETRY_INVENTORY:
            return stage("retry", object_id)
        case NextActionKind.COMMIT:
            return stage("commit", object_id)
        case NextActionKind.NONE:
            return ""
