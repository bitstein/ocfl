import shlex

from ocfl_cli.model.metadata import NextActionKind


def stage(verb: str, *args: str) -> str:
    return " ".join(["ocfl stage", verb, *(shlex.quote(a) for a in args)])


def batch(verb: str, batch_id: str, *flags: str) -> str:
    return stage("batch", verb, batch_id, *flags)


def next_command(kind: NextActionKind, object_id: str) -> str:
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
