from copy import deepcopy

import test_pr_lifecycle_workstream as base


class LandingGitHub(base.MultiGitHub):
    def __init__(self):
        super().__init__()
        self.target_head = "f" * 40
        self.target_ancestors: set[str] = set()

    def get_ref_sha(self, ref="heads/main"):
        assert ref == "heads/main"
        return self.target_head

    def is_ancestor(self, ancestor, descendant):
        assert descendant == self.target_head
        return ancestor in self.target_ancestors


def _review_then_merge_stack(github):
    lifecycle = base.engine(github)
    candidate = base.candidate_for(lifecycle, github)
    base.add_workstream_reviews(github, candidate, "MERGE")
    for number in base.NUMBERS:
        github.prs[number]["state"] = "closed"
        github.prs[number]["merged"] = True
        github.prs[number]["merged_at"] = base.NOW.isoformat()
    values = [lifecycle.inspect(github.get_pr(number)) for number in base.NUMBERS]
    return lifecycle, values


def test_intermediate_merges_remain_one_cumulative_recovery_until_main_contains_them():
    github = LandingGitHub()
    lifecycle, values = _review_then_merge_stack(github)

    candidate = lifecycle._workstream_candidates(values)[base.WORKSTREAM_TASK]

    assert [member.publication_state for member in candidate.members] == [
        "landed",
        "merged",
        "merged",
        "merged",
    ]
    assert candidate.source_complete is False
    assert candidate.recovery_source is not None
    assert candidate.recovery_source.pr_number == 160
    assert candidate.recovery_source.head == base.HEADS[-1]
    assert base.workstream.current_review_state(candidate, github).status == "merge"

    github.target_ancestors.update(base.HEADS[1:])
    landed = lifecycle._workstream_candidates(values)[base.WORKSTREAM_TASK]

    assert all(member.publication_state == "landed" for member in landed.members)
    assert landed.source_complete is True
    assert landed.recovery_source is None
    assert base.workstream.current_review_state(landed, github).status == "merge"


def test_ultimate_target_is_explicit_and_part_of_workstream_shape_identity():
    github = LandingGitHub()
    lifecycle = base.engine(github)
    main_candidate = base.candidate_for(lifecycle, github)

    for number in base.NUMBERS:
        github.prs[number]["body"] = github.prs[number]["body"].replace(
            " total=4 -->", " total=4 target=release -->"
        )
    release_values = [lifecycle.inspect(deepcopy(github.get_pr(number))) for number in base.NUMBERS]
    release_candidate = lifecycle._workstream_candidates(release_values)[base.WORKSTREAM_TASK]

    assert {member.ultimate_target for member in release_candidate.members} == {"release"}
    assert release_candidate.shape_id != main_candidate.shape_id
