from typing import NamedTuple
from enum import Enum

from repodynamics.datatype import BranchType


class RulesetEnforcementLevel(Enum):
    """
    The enforcement level of the branch protection ruleset.

    Attributes
    ----------
    ENABLED : str
        The ruleset is enabled.
    DISABLED : str
        The ruleset is disabled.
    EVALUATE : str
        The ruleset is in evaluation, allowing admins to test rules before enforcing them.
    """
    ENABLED = "enabled"
    DISABLED = "disabled"
    EVALUATE = "evaluate"


class RulesetBypassActorType(Enum):
    """
    The type of actor that can bypass the branch protection ruleset.
    """
    ORG_ADMIN = "organization_admin"
    REPO_ROLE = "repository_role"
    TEAM = "team"
    INTEGRATION = "integration"


class RulesetBypassMode(Enum):
    """
    The mode of bypass for the branch protection ruleset.
    """
    ALWAYS = "always"
    PULL = "pull_request"


class RulesetBypassActor(NamedTuple):
    id: int
    type: RulesetBypassActorType
    mode: RulesetBypassMode


class RulesetStatusCheckContext(NamedTuple):
    name: str
    integration_id: int | None


class Rules(NamedTuple):
    protect_creation: bool
    protect_deletion: bool
    protect_modification: bool
    modification_allows_fetch_and_merge: bool | None
    protect_force_push: bool
    require_linear_history: bool
    require_signatures: bool
    require_pull_request: bool
    dismiss_stale_reviews_on_push: bool | None
    require_code_owner_review: bool | None
    require_last_push_approval: bool | None
    require_review_thread_resolution: bool | None
    required_approving_review_count: int | None
    require_status_checks: bool
    status_check_contexts: tuple[RulesetStatusCheckContext, ...]
    status_check_strict_policy: bool | None
    required_deployment_environments: tuple[str, ...]


class BranchProtectionRuleset(NamedTuple):
    enforcement: RulesetEnforcementLevel
    bypass_actors: tuple[RulesetBypassActor, ...]
    rule: Rules


class MainBranch(NamedTuple):
    name: str
    ruleset: BranchProtectionRuleset


class GroupedBranch(NamedTuple):
    prefix: str
    ruleset: BranchProtectionRuleset


class Branch:

    def __init__(self, options: dict):

        def instantiate_ruleset(ruleset: dict) -> BranchProtectionRuleset:
            return BranchProtectionRuleset(
                enforcement=RulesetEnforcementLevel(ruleset["enforcement"]),
                bypass_actors=tuple(
                    RulesetBypassActor(
                        id=actor["id"],
                        type=RulesetBypassActorType(actor["type"]),
                        mode=RulesetBypassMode(actor["mode"])
                    ) for actor in ruleset["bypass_actors"]
                ),
                rule=Rules(
                    protect_creation=ruleset["rule"].get("protect_creation", False),
                    protect_deletion=ruleset["rule"].get("protect_deletion", False),
                    protect_modification="protect_modification" in ruleset["rule"],
                    modification_allows_fetch_and_merge=ruleset["rule"].get(
                        "protect_modification", {}
                    ).get("allow_fetch_and_merge"),
                    protect_force_push=ruleset["rule"].get("protect_force_push", False),
                    require_linear_history=ruleset["rule"].get("require_linear_history", False),
                    require_signatures=ruleset["rule"].get("require_signatures", False),
                    require_pull_request="require_pull_request" in ruleset["rule"],
                    dismiss_stale_reviews_on_push=ruleset["rule"].get(
                        "require_pull_request", {}
                    ).get("dismiss_stale_reviews_on_push"),
                    require_code_owner_review=ruleset["rule"].get(
                        "require_pull_request", {}
                    ).get("require_code_owner_review"),
                    require_last_push_approval=ruleset["rule"].get(
                        "require_pull_request", {}
                    ).get("require_last_push_approval"),
                    require_review_thread_resolution=ruleset["rule"].get(
                        "require_pull_request", {}
                    ).get("require_review_thread_resolution"),
                    required_approving_review_count=ruleset["rule"].get(
                        "require_pull_request", {}
                    ).get("required_approving_review_count"),
                    require_status_checks="require_status_checks" in ruleset["rule"],
                    status_check_contexts=tuple(
                        RulesetStatusCheckContext(
                            name=context["name"],
                            integration_id=context.get("integration_id")
                        ) for context in
                        ruleset["rule"].get("require_status_checks", {}).get("contexts", [])
                    ),
                    status_check_strict_policy=ruleset["rule"].get(
                        "require_status_checks", {}
                    ).get("strict"),
                    required_deployment_environments=tuple(
                        ruleset["rule"].get("required_deployment_environments", [])
                    )
                )
            )

        self._options = options
        self._branch_main = MainBranch(
            name=options["branch"]["main"]["name"],
            ruleset=instantiate_ruleset(options["branch"]["main"]["ruleset"])
        )
        for branch_group in ("release", "pre-release", "implementation", "development", "auto-update"):
            setattr(
                self,
                f"_branch_{branch_group.replace('-', '_')}",
                GroupedBranch(
                    prefix=options["branch"][branch_group]["prefix"],
                    ruleset=instantiate_ruleset(options["branch"][branch_group]["ruleset"])
                )
            )
        return

    @property
    def main(self) -> MainBranch:
        return self._branch_main

    @property
    def release(self) -> GroupedBranch:
        return self._branch_release

    @property
    def pre_release(self) -> GroupedBranch:
        return self._branch_pre_release

    @property
    def implementation(self) -> GroupedBranch:
        return self._branch_implementation

    @property
    def development(self) -> GroupedBranch:
        return self._branch_development

    @property
    def auto_update(self) -> GroupedBranch:
        return self._branch_auto_update

    @property
    def groups(self) -> dict[BranchType, GroupedBranch]:
        return {
            BranchType.RELEASE: self.release,
            BranchType.PRERELEASE: self.pre_release,
            BranchType.IMPLEMENT: self.implementation,
            BranchType.DEV: self.development,
            BranchType.AUTOUPDATE: self.auto_update
        }
