from pathlib import Path
import json
from typing import Literal, Optional
import re

from markitup import html, md
import pylinks

import repodynamics as rd
from repodynamics.logger import Logger
from repodynamics.git import Git
from repodynamics import meta
from repodynamics import hooks, _util
import repodynamics.commits


def init(context: dict, logger=None):
    match context["event_name"]:
        case "schedule", "workflow_dispatch":
            return Dispatch(context=context).run()
        case "issue":
            return Issue(context=context).run()
        case "pull_request":
            return Pull(context=context).run()
        case "push":
            if context["event"]["created"]:
                logger.error("Creation Event: Skipping.")
                return None, None, None
            ref = context["ref_name"]
            if ref == "main" or ref.startswith("release/"):
                logger.success("Detected Event: Push to release branch")
                return PushRelease(context=context).run()
            elif ref.startswith("dev/"):
                logger.success("Detected Event: Push to development branch")
                return PushDev(context=context).run()
        case _:
            logger.error(f"Event '{context['event_name']}' is not supported.")
    return


def pull_post_process(pull):
    pr_nr = pull["pull-request-number"]
    pr_url = pull["pull-request-url"]
    pr_head_sha = pull["pull-request-head-sha"]


class EventHandler:

    def __init__(self, context: dict):
        self._context: dict = context
        self._logger: Logger = Logger("github")
        self._git: Git = Git(logger=self._logger)
        self._api = pylinks.api.github(token=context["token"]).user(self.repo_owner).repo(self.repo_name)
        self._metadata: dict = meta.load(logger=self._logger)
        self._latest_version = self._git.describe()

        self._output_meta: dict = {}
        self._output_hooks: dict = {}
        return

    @property
    def context(self) -> dict:
        """The 'github' context of the triggering event.

        References
        ----------
        - [GitHub Docs](https://docs.github.com/en/actions/learn-github-actions/contexts#github-context)
        """
        return self._context

    @property
    def payload(self) -> dict:
        """The full webhook payload of the triggering event.

        References
        ----------
        - [GitHub Docs](https://docs.github.com/en/webhooks/webhook-events-and-payloads)
        """
        return self.context["event"]

    @property
    def event_name(self) -> str:
        """The name of the triggering event, e.g. 'push', 'pull_request' etc."""
        return self.context["event_name"]

    @property
    def ref_name(self) -> str:
        """The short ref name of the branch or tag that triggered the event, e.g. 'main', 'dev/1' etc."""
        return self.context["ref_name"]

    @property
    def repo_owner(self) -> str:
        """GitHub username of the repository owner."""
        return self.context["repository_owner"]

    @property
    def repo_name(self) -> str:
        """Name of the repository."""
        return self.context["repository"].removeprefix(f"{self.repo_owner}/")


    def _update_meta(self, action: Literal['fail', 'amend', 'commit']):
        self._output_meta = meta.update(
            action="report" if action == "fail" else action,
            github_token=self._context["token"],
            logger=self._logger,
        )
        if action == "fail" and not self._output_meta["passed"]:
            self._logger.error("Dynamic files are not in sync.")
        self._metadata = self._output_meta["metadata"]
        return

    def _update_hooks(self, action: Literal['fail', 'amend', 'commit'], ref_range: Optional[tuple[str, str]] = None):
        self._output_hooks = hooks.run(
            action="report" if action == "fail" else action,
            ref_range=ref_range,
            path_config=self._metadata["workflow_hooks_config_path"],
            logger=self._logger,
        )
        if action == "fail" and not self._output_hooks["passed"]:
            self._logger.error("Hooks failed.")
        return

    @staticmethod
    def _process_changes(changes):
        """

        Parameters
        ----------
        changes

        Returns
        -------
        The keys of the JSON dictionary are the groups that the files belong to,
        defined in `.github/config/changed_files.yaml`. Another key is `all`, which is added as extra
        (i.e. without being defined in the config file), which contains details on changes in the entire repository.
        Each value is then a dictionary itself, as defined in the action's documentation.

        Notes
        -----
        The boolean values in the output are given as strings, i.e. `true` and `false`.

        References
        ----------
        - https://github.com/marketplace/actions/changed-files
        """
        sep_groups = dict()
        for item_name, val in changes.items():
            group_name, attr = item_name.split("_", 1)
            group = sep_groups.setdefault(group_name, dict())
            group[attr] = val
        for group_name, group_attrs in sep_groups.items():
            sep_groups[group_name] = dict(sorted(group_attrs.items()))
        return sep_groups

    def assemble_summary(self):
        sections = [
            html.h(2, "Summary"),
            self.summary,
        ]
        if self.changes:
            sections.append(self.changed_files())
        for job, summary_filepath in zip(
            (self.meta, self.hooks),
            (".local/reports/repodynamics/meta.md", ".local/reports/repodynamics/hooks.md")
        ):
            if job:
                with open(summary_filepath) as f:
                    sections.append(f.read())
        return html.ElementCollection(sections)

    def changed_files_summary(self):
        summary = html.ElementCollection(
            [
                html.h(2, "Changed Files"),
            ]
        )
        for group_name, group_attrs in self.changes.items():
            if group_attrs["any_modified"] == "true":
                summary.append(
                    html.details(
                        content=md.code_block(json.dumps(group_attrs, indent=4), "json"),
                        summary=group_name,
                    )
                )
            # group_summary_list.append(
            #     f"{'✅' if group_attrs['any_modified'] == 'true' else '❌'}  {group_name}"
            # )
        file_list = "\n".join(sorted(self.changes["all"]["all_changed_and_modified_files"].split()))
        # Write job summary
        summary.append(
            html.details(
                content=md.code_block(file_list, "bash"),
                summary="🖥 Changed Files",
            )
        )
        # details = html.details(
        #     content=md.code_block(json.dumps(all_groups, indent=4), "json"),
        #     summary="🖥 Details",
        # )
        # log = html.ElementCollection(
        #     [html.h(4, "Modified Categories"), html.ul(group_summary_list), changed_files, details]
        # )
        return summary


class Push(EventHandler):
    """
    References
    ----------
    - [GitHub Docs: Webhook events & payloads](https://docs.github.com/en/webhooks/webhook-events-and-payloads#push)
    """

    def __init__(self, context: dict):
        super().__init__(context)
        self._changes = self._get_changed_files()
        self._commits = self._get_commits()
        self._head_commit_msg: str = self.payload["head_commit"]["message"]
        return

    @property
    def hash_before(self) -> str:
        """The SHA hash of the most recent commit on the branch before the push."""
        return self.payload["before"]

    @property
    def hash_after(self) -> str:
        """The SHA hash of the most recent commit on the branch after the push."""
        return self.payload["after"]

    @property
    def changed_files(self) -> list[str]:
        """List of changed files."""
        return self._changes

    def _get_commits(self) -> list[dict]:
        commits = self._git.get_commits(f"{self.hash_before}..{self.hash_after}")
        for commit in commits:
            conventional_commit = rd.commits.parse(msg=commit["message"], types=types, logger=self._logger)

    def _get_tags(self):


    def _get_changed_files(self) -> list[str]:
        filepaths = []
        changes = self._git.changed_files(ref_start=self.hash_before, ref_end=self.hash_after)
        for change_type, changed_paths in changes.items():
            if change_type in ["unknown", "broken"]:
                self._logger.warning(
                    f"Found {change_type} files",
                    f"Running 'git diff' revealed {change_type} changes at: {changed_paths}. "
                    "These files will be ignored."
                )
                continue
            if change_type.startswith("copied") and change_type.endswith("from"):
                continue
            filepaths.extend(changed_paths)
        return filepaths




class PushRelease(Push):

    def __init__(self, context: dict, changes: dict):
        super().__init__(context, changes)

        self._head_commit_conv: dict = self._parse_commit_msg(self._head_commit_msg)
        if not self._head_commit_conv:
            self._logger.error(f"Invalid commit message: {self._head_commit_msg}")


        self._output = {
            "config-repo": False,

        }
        self._version_new = None
        return

    def _determine_commit_type(self):
        if self._head_commit_conv["breaking"]:
            return "release_major"


        return


    def run(self):
        self._update_meta(action="report")
        if not self._output_meta["passed"]:
            self._logger.error("Dynamic files are not in sync .")

        semver_tag: tuple[int, int, int] | None = self._git.describe()
        if not semver_tag:
            return self._run_initial_release()

        if commit_msg.startswith("feat"):
            release = True
            self._tag = f"v{semver_tag[0]}.{semver_tag[1] + 1}.0"
        elif commit_msg.startswith("fix"):
            release = "patch"
            self._tag = f"v{semver_tag[0]}.{semver_tag[1]}.{semver_tag[2] + 1}"

        output = {
            "hash": self._tag,
            "publish": True,
            "docs": True,
            "website_url": self._metadata['url']['website']['base'],
            "name": self._metadata['name']
        }
        return output, None, None

    def _run_initial_release(self):
        self._tag = "v0.0.0"
        self._create_changelog()
        self._git.commit(
            stage='all',
            amend=True,
        )
        self._git.push(force_with_lease=True)
        self._git.create_tag(tag="v0.0.0", message="Initial Release")
        output = {
            "hash": self._tag,
            "publish": True,
            "docs": True,
            "website_url": self._metadata['url']['website']['base'],
            "name": self._metadata['name']
        }
        self._create_release_notes()
        return output, None, None

    def _create_changelog(self):
        changelog = f"""# Changelog
This document tracks all changes to the {self._metadata['name']} public API.

## [{self._metadata['name']} {self._tag.removesuffix('v')}]({self._metadata['url']['github']['releases']['home']}/tag/{self._tag})
This is the initial release of the project. Infrastructure is now in place to support future releases.
"""
        with open("CHANGELOG.md", "w") as f:
            f.write(changelog)
        return

    def _create_release_notes(self):
        path = Path(".local/temp/repodynamics/release_notes.md")
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            f.write(f"[{self._metadata['name']} {self._tag.removesuffix('v')}]({self._metadata['url']['github']['releases']['home']}/tag/{self._tag})")
        return

    def _create_changelog_entry(self):
        log = f"""## [{self._metadata['name']} {self._tag}]()"""

        return


class PushDev(Push):

    def __init__(self, context: dict, changes: dict):
        super().__init__(context, changes)
        issue_nr = self.ref_name.removeprefix("dev/")
        if not issue_nr.isdigit():
            self._logger.error(f"Invalid branch name: {self.ref_name}")
        self._issue_nr = int(issue_nr)
        try:
            self._issue = self._api.issue(self._issue_nr)
        except pylinks.http.WebAPIStatusCodeError as e:
            if e.status_code == 404:
                self._logger.error(f"Invalid issue number: {self._issue_nr}")
            else:
                self._logger.error(f"Could not retrieve issue data", e)
        if self._issue['state'] != 'open':
            self._logger.error(f"Issue #{self._issue_nr} is not open.")
        self._issue_type = self.issue_type()
        return

    def issue_type(self):
        type_labels = []
        for label in self._issue["labels"]:
            if label["name"].startswith("Type: "):
                type_labels.append(label["name"])
        if len(type_labels) == 0:
            self._logger.error(f"Could not determine issue type for issue #{self._issue_nr}")
        elif len(type_labels) > 1:
            self._logger.error(f"Found multiple issue types for issue #{self._issue_nr}: {type_labels}")
        return type_labels[0].removeprefix("Type: ")

    def run(self):
        summary = html.ElementCollection()
        output = {
            "package_test": self.package_test_needed,
            "package_lint": self.package_lint_needed,
            "docs": self.docs_test_needed,
        }
        if self._changes["meta"]["any_modified"] == "true":
            self._update_meta(action=self)
            summary.append(self._output_meta["summary"])
            if self._output_meta["changes"].get("package"):
                output["package_test"] = True
                output["package_lint"] = True
                output["docs"] = True
            if self._output_meta["changes"].get("metadata"):
                output["docs"] = True
            self._metadata = self._output_meta["metadata"]
            hash_after = self._output_meta["commit_hash"] or hash_after

        if self._metadata.get("workflow_hooks_config_path"):
            self._update_hooks(action="commit", ref_range=(self._hash_before, self._hash_after))
            summary.append(self._output_hooks["summary"])
        else:
            self._logger.attention("No workflow hooks config path set in metadata.")


        output["hash"] = self._git.push()
        return output, None, str(summary)

    @property
    def package_test_needed(self):
        return any(
            self._changes[group]["any_modified"] == "true"
            for group in ["src", "tests", "setup-files", "workflow"]
        )

    @property
    def package_lint_needed(self):
        for group in ["src", "setup-files", "workflow"]:
            if self._changes[group]["any_modified"] == "true":
                return True
        return False

    @property
    def docs_test_needed(self):
        return any(
            self._changes[group]["any_modified"] == "true"
            for group in ["src", "docs-website", "workflow"]
        )


class InitialRelease(EventHandler):

    def __init__(self, context: dict):
        super().__init__(context)
        return

    def run(self):
        return


class Pull(EventHandler):

    def __init__(self, context: dict, changes: dict):
        super().__init__(context)



        self._changes = self._process_changes(changes)
        self._output = {}
        return

    def run(self):
        hash_before = self.payload['pull_request']['base']['sha']
        hash_after = self.payload['pull_request']['head']['sha']
        self._update_meta(action="report")
        self._update_hooks(action="report", ref_range=(hash_before, hash_after))
        return

    @property
    def info(self) -> dict:
        return self.payload["pull_request"]

    @property
    def label_names(self):
        return [label["name"] for label in self.info["labels"]]

    @property
    def triggering_action(self) -> str:
        """Pull-request action type that triggered the event, e.g. 'opened', 'closed', 'reopened' etc."""
        return self.payload["action"]

    @property
    def number(self) -> int:
        """Pull-request number."""
        return self.payload["number"]

    @property
    def head(self) -> dict:
        """Pull request's head branch info."""
        return self.info["head"]

    @property
    def base(self) -> dict:
        """Pull request's base branch info."""
        return self.info["base"]

    @property
    def internal(self) -> bool:
        """Whether the pull request is internal, i.e. within the same repository."""
        return self.head["repo"]["full_name"] == self._context["repository"]

    @property
    def body(self) -> str | None:
        """Pull request body."""
        return self.info["body"]

    @property
    def title(self) -> str:
        """Pull request title."""
        return self.info["title"]

    @property
    def state(self) -> Literal["open", "closed"]:
        """Pull request state; either 'open' or 'closed'."""
        return self.info["state"]

    @property
    def merged(self) -> bool:
        """Whether the pull request is merged."""
        return self.state == 'closed' and self.info["merged"]


class Issue(EventHandler):

    def __init__(self, context: dict):
        super().__init__(context)
        self._output = {}
        return

    def run(self):
        return


class IssueComment(EventHandler):

    def __init__(self, context: dict):
        super().__init__(context)
        self._output = {}
        return

    def run(self):
        self.run_commands()
        return

    def run_commands(self):
        runner = {
            "test-package": self.command_test_package,
        }
        command, kwargs = self.find_command()
        if not command:
            return
        if command not in runner:
            self._logger.error(f"Command '{command}' is not supported.")
            return
        runner[command](**kwargs)
        return

    def command_test_package(self, operating_systems):

    def find_command(self) -> tuple[str, dict]:
        pattern_general = r"""
        ^@RepoDynamicsBot[ ]     # Matches the string at the start of a line with a space after "Bot"
        (?P<command>[^\n]+)      # Captures the command until a new line
        \n                       # Matches the newline character after the command
        (?P<arguments>.*)        # Captures everything that comes after the newline
        """
        command_match = re.search(pattern_general, self.comment_body, re.MULTILINE | re.DOTALL | re.VERBOSE)
        if not command_match:
            return "", {}
        pattern_arguments = r"""
        (?P<key>[a-zA-Z][a-zA-Z0-9_-]*)  # Captures the key
        :[ \t]*                          # Colon separator, possibly followed by spaces/tabs
        (?:
            `(?P<value1>[^`]+)`          # Value enclosed in backticks, capturing at least one character
            |                            # OR
            \n```[\w-]*\s*                 # Captures the optional language specifier
            (?P<value2>.+?)              # Captures the content of the multiline code block non-greedily
            \n```                          # Ending delimiter of the multiline code block
        )
        """
        kv_dict = {
            match.group("key"): match.group("value1") or match.group("value2")
            for match in re.finditer(
                pattern_arguments, command_match.group("arguments"), re.MULTILINE | re.DOTALL | re.VERBOSE
            )
        }
        return command_match.group("command"), kv_dict

    @property
    def triggering_action(self) -> str:
        """Comment action type that triggered the event; one of 'created', 'deleted', 'edited'."""
        return self.payload["action"]

    @property
    def comment(self) -> dict:
        """Comment data."""
        return self.payload["comment"]

    @property
    def comment_body(self) -> str:
        """Comment body."""
        return self.comment["body"]

    @property
    def comment_id(self) -> int:
        """Unique identifier of the comment."""
        return self.comment["id"]

    @property
    def commenter(self) -> str:
        """Commenter username."""
        return self.comment["user"]["login"]

    @property
    def issue(self) -> dict:
        """Issue data."""
        return self.payload["issue"]


class Dispatch(EventHandler):

    def __init__(self, context: dict):
        super().__init__(context)
        self._output = {"pull": True}
        return

    def run(self):
        summary = html.ElementCollection()
        self._update_meta(action="commit")
        summary.append(self._output_meta["summary"])
        if self._metadata.get("workflow_hooks_config_path"):
            self._update_hooks(action="commit")
            summary.append(self._output_hooks["summary"])
        self._create_pull_body()
        return self._output, None, str(summary)

    def _create_pull_body(self):
        path = Path(".local/temp/repodynamics/init/pr_body.md")
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            f.write("Automatically update all metadata and dynamic files.\nRun hooks.")
        return





def _finalize(
    context: dict,
    changes: dict,
    meta_changes: dict,
    commit_meta: bool,
    hooks_check: str,
    hooks_fix: str,
    commit_hooks: bool,
    push_ref: str,
    pull_number: str,
    pull_url: str,
    pull_head_sha: str,
    logger: Logger = None,
) -> tuple[dict, str]:
    """
    Parse outputs from `actions/changed-files` action.

    This is used in the `repo_changed_files.yaml` workflow.
    It parses the outputs from the `actions/changed-files` action and
    creates a new output variable `json` that contains all the data,
    and writes a job summary.
    """
    job_summary = html.ElementCollection()

    job_summary.append(html.h(2, "Metadata"))

    with open("meta/.out/metadata.json") as f:
        metadata_dict = json.load(f)

    job_summary.append(
        html.details(
            content=md.code_block(json.dumps(metadata_dict, indent=4), "json"),
            summary=" 🖥  Metadata",
        )
    )

    job_summary.append(
        html.details(
            content=md.code_block(json.dumps(summary_dict, indent=4), "json"),
            summary=" 🖥  Summary",
        )
    )

    force_update_emoji = "✅" if force_update == "all" else ("❌" if force_update == "none" else "☑️")
    cache_hit_emoji = "✅" if cache_hit else "❌"
    if not cache_hit or force_update == "all":
        result = "Updated all metadata"
    elif force_update == "core":
        result = "Updated core metadata but loaded API metadata from cache"
    else:
        result = "Loaded all metadata from cache"

    results_list = html.ElementCollection(
        [
            html.li(f"{force_update_emoji}  Force update (input: {force_update})", content_indent=""),
            html.li(f"{cache_hit_emoji}  Cache hit", content_indent=""),
            html.li(f"➡️  {result}", content_indent=""),
        ],
    )
    log = f"<h2>Repository Metadata</h2>{metadata_details}{results_list}"

    return {"json": json.dumps(all_groups)}, str(log)


def find_command(comment: str) -> tuple[str, dict] | None:
    """
    Find and parse a RepoDynamicsBot command in a comment.

    The comment must only contain one command.
    The command must start with '@RepoDynamicsBot ' at the beginning of a line,
    followed by the command. If the command requires input arguments, they must
    be given in the next line(s).

    Parameters
    ----------
    comment : str

    Returns
    -------
    command, kwargs : tuple[str, dict]

    Examples
    --------
    A sample comment:

    @RepoDynamicsBot test
    operating-systems: `['ubuntu-latest', 'windows-latest']`
    python-versions: `['3.11', '3.10']`
    run:
    ```python
    import package
    print(package.__version__)
    ```

    """
    pattern_general = r"""
    ^@RepoDynamicsBot[ ]     # Matches the string at the start of a line with a space after "Bot"
    (?P<command>[^\n]+)      # Captures the command until a new line
    \n                       # Matches the newline character after the command
    (?P<arguments>.*)        # Captures everything that comes after the newline in a group called "arguments"
    """
    command_match = re.search(pattern_general, comment, re.MULTILINE | re.DOTALL | re.VERBOSE)
    if not command_match:
        return
    pattern_arguments = r"""
        (?P<key>[a-zA-Z][a-zA-Z0-9_-]*)  # Captures the key
        :[ \t]*                          # Colon separator, possibly followed by spaces/tabs
        (?:
            `(?P<value1>[^`]+)`          # Value enclosed in backticks, capturing at least one character
            |                            # OR
            \n```[\w-]*\s*                 # Captures the optional language specifier
            (?P<value2>.+?)              # Captures the content of the multiline code block non-greedily
            \n```                          # Ending delimiter of the multiline code block
        )
        """
    kv_matches = re.finditer(
        pattern_arguments, command_match.group("arguments"), re.MULTILINE | re.DOTALL | re.VERBOSE
    )
    kv_dict = {}
    for match in kv_matches:
        kv_dict[match.group("key")] = match.group("value1") or match.group("value2")
    return command_match.group("command"), kv_dict