from repodynamics.files.package import python


def sync(sync_manager):
    language = sync_manager.metadata["package"]["language"]
    match language:
        case "python":
            python.Python(sync_manager=sync_manager).update()
        case _:
            raise NotImplementedError(f"Language '{language}' not implemented.")
    return