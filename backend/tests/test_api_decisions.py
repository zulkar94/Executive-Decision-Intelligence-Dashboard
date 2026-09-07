"""Decision CRUD, matrix integrity, and the ranking/sensitivity/simulate flow."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from tests.conftest import VIEWER, auth_headers, login, register


async def test_create_read_update_delete_decision(client: AsyncClient, admin_headers) -> None:
    created = await client.post(
        "/api/v1/decisions", json={"title": "Build or buy"}, headers=admin_headers
    )
    assert created.status_code == 201
    decision_id = created.json()["id"]

    fetched = await client.get(f"/api/v1/decisions/{decision_id}", headers=admin_headers)
    assert fetched.json()["status"] == "draft"

    patched = await client.patch(
        f"/api/v1/decisions/{decision_id}", json={"status": "decided"}, headers=admin_headers
    )
    assert patched.json()["status"] == "decided"

    assert (
        await client.delete(f"/api/v1/decisions/{decision_id}", headers=admin_headers)
    ).status_code == 204
    assert (
        await client.get(f"/api/v1/decisions/{decision_id}", headers=admin_headers)
    ).status_code == 404


async def test_title_is_length_validated(client: AsyncClient, admin_headers) -> None:
    assert (
        await client.post("/api/v1/decisions", json={"title": "ab"}, headers=admin_headers)
    ).status_code == 422
    assert (
        await client.post("/api/v1/decisions", json={"title": "x" * 400}, headers=admin_headers)
    ).status_code == 422


async def test_duplicate_criterion_name_conflicts(
    client: AsyncClient, admin_headers, decision
) -> None:
    response = await client.post(
        f"/api/v1/decisions/{decision['id']}/criteria",
        json={"name": "Cost", "weight": 0.4, "direction": "cost"},
        headers=admin_headers,
    )
    assert response.status_code == 409


async def test_unknown_decision_returns_404(client: AsyncClient, admin_headers) -> None:
    assert (
        await client.get("/api/v1/decisions/deadbeef", headers=admin_headers)
    ).status_code == 404


async def test_another_users_decision_is_indistinguishable_from_missing(
    client: AsyncClient, admin_headers, decision
) -> None:
    """A non-owner must get 404, not 403, so ids cannot be probed."""
    await register(client, VIEWER)
    users = (await client.get("/api/v1/admin/users", headers=admin_headers)).json()
    viewer = next(user for user in users if user["email"] == VIEWER["email"])
    await client.patch(
        f"/api/v1/admin/users/{viewer['id']}/role", json={"role": "analyst"}, headers=admin_headers
    )
    other = auth_headers(await login(client, VIEWER))

    seen = await client.get(f"/api/v1/decisions/{decision['id']}", headers=other)
    missing = await client.get("/api/v1/decisions/0123456789abcdef", headers=other)
    assert seen.status_code == missing.status_code == 404


async def test_scores_cannot_reference_a_foreign_decision(
    client: AsyncClient, admin_headers, decision
) -> None:
    other = await client.post(
        "/api/v1/decisions", json={"title": "Unrelated decision"}, headers=admin_headers
    )
    other_id = other.json()["id"]
    foreign_option = await client.post(
        f"/api/v1/decisions/{other_id}/options", json={"name": "Foreign"}, headers=admin_headers
    )
    response = await client.put(
        f"/api/v1/decisions/{decision['id']}/scores",
        json={
            "scores": [
                {
                    "option_id": foreign_option.json()["id"],
                    "criterion_id": decision["criteria"][0]["id"],
                    "value": 1.0,
                }
            ]
        },
        headers=admin_headers,
    )
    assert response.status_code == 422


async def test_score_upsert_is_idempotent(client: AsyncClient, admin_headers, decision) -> None:
    payload = {
        "scores": [
            {
                "option_id": decision["options"][0]["id"],
                "criterion_id": decision["criteria"][0]["id"],
                "value": 42.0,
                "uncertainty": 1.0,
            }
        ]
    }
    for _ in range(3):
        response = await client.put(
            f"/api/v1/decisions/{decision['id']}/scores", json=payload, headers=admin_headers
        )
        assert response.status_code == 200
    scores = (
        await client.get(f"/api/v1/decisions/{decision['id']}/scores", headers=admin_headers)
    ).json()
    assert len(scores) == 9  # unchanged cell count
    updated = next(
        score
        for score in scores
        if score["option_id"] == decision["options"][0]["id"]
        and score["criterion_id"] == decision["criteria"][0]["id"]
    )
    assert updated["value"] == 42.0


async def test_clearing_scores_empties_the_matrix(
    client: AsyncClient, admin_headers, decision
) -> None:
    assert (
        await client.delete(f"/api/v1/decisions/{decision['id']}/scores", headers=admin_headers)
    ).status_code == 204
    scores = (
        await client.get(f"/api/v1/decisions/{decision['id']}/scores", headers=admin_headers)
    ).json()
    assert scores == []


async def test_ranking_returns_a_complete_ordering(
    client: AsyncClient, admin_headers, decision
) -> None:
    response = await client.get(
        f"/api/v1/decisions/{decision['id']}/ranking", headers=admin_headers
    )
    assert response.status_code == 200
    body = response.json()
    assert [row["rank"] for row in body["ranked"]] == sorted(row["rank"] for row in body["ranked"])
    assert len(body["ranked"]) == 3
    assert sum(body["weights"].values()) == pytest.approx(1.0)
    assert set(body["ranked"][0]["contributions"]) == set(body["weights"])


async def test_topsis_is_available_and_bounded(
    client: AsyncClient, admin_headers, decision
) -> None:
    body = (
        await client.get(
            f"/api/v1/decisions/{decision['id']}/ranking?method=topsis", headers=admin_headers
        )
    ).json()
    assert all(0.0 <= row["score"] <= 1.0 for row in body["ranked"])


async def test_unknown_method_is_rejected(client: AsyncClient, admin_headers, decision) -> None:
    response = await client.get(
        f"/api/v1/decisions/{decision['id']}/ranking?method=borda", headers=admin_headers
    )
    assert response.status_code == 422


async def test_ranking_needs_at_least_two_options(client: AsyncClient, admin_headers) -> None:
    created = await client.post(
        "/api/v1/decisions", json={"title": "Only one path"}, headers=admin_headers
    )
    decision_id = created.json()["id"]
    await client.post(
        f"/api/v1/decisions/{decision_id}/options", json={"name": "Solo"}, headers=admin_headers
    )
    response = await client.get(f"/api/v1/decisions/{decision_id}/ranking", headers=admin_headers)
    assert response.status_code == 409


async def test_sensitivity_lists_every_criterion(
    client: AsyncClient, admin_headers, decision
) -> None:
    body = (
        await client.get(
            f"/api/v1/decisions/{decision['id']}/sensitivity?steps=15", headers=admin_headers
        )
    ).json()
    assert len(body["rows"]) == 3
    assert {row["criterion"] for row in body["rows"]} == {"Cost", "Speed", "Risk"}
    assert body["incumbent_option"] in {"Alpha", "Bravo", "Charlie"}


async def test_simulation_is_reproducible_over_http(
    client: AsyncClient, admin_headers, decision
) -> None:
    payload = {"runs": 600, "seed": 42, "weight_jitter": 0.1}
    first = (
        await client.post(
            f"/api/v1/decisions/{decision['id']}/simulate", json=payload, headers=admin_headers
        )
    ).json()
    second = (
        await client.post(
            f"/api/v1/decisions/{decision['id']}/simulate", json=payload, headers=admin_headers
        )
    ).json()
    assert first["options"] == second["options"]
    assert 0.0 <= first["decision_confidence"] <= 1.0


async def test_simulation_run_count_is_bounded(
    client: AsyncClient, admin_headers, decision
) -> None:
    response = await client.post(
        f"/api/v1/decisions/{decision['id']}/simulate",
        json={"runs": 10_000_000},
        headers=admin_headers,
    )
    assert response.status_code == 422


async def test_simulation_refuses_a_matrix_with_no_uncertainty(
    client: AsyncClient, admin_headers, decision
) -> None:
    zeroed = {
        "scores": [
            {
                "option_id": option["id"],
                "criterion_id": criterion["id"],
                "value": 5.0,
                "uncertainty": 0.0,
            }
            for option in decision["options"]
            for criterion in decision["criteria"]
        ]
    }
    await client.put(
        f"/api/v1/decisions/{decision['id']}/scores", json=zeroed, headers=admin_headers
    )
    response = await client.post(
        f"/api/v1/decisions/{decision['id']}/simulate",
        json={"runs": 200, "weight_jitter": 0.0},
        headers=admin_headers,
    )
    assert response.status_code == 409


async def test_ahp_applies_consistent_weights(client: AsyncClient, admin_headers, decision) -> None:
    pairwise = [[1.0, 3.0, 5.0], [1 / 3, 1.0, 2.0], [0.2, 0.5, 1.0]]
    response = await client.post(
        f"/api/v1/decisions/{decision['id']}/weights/ahp",
        json={"matrix": pairwise, "apply_weights": True},
        headers=admin_headers,
    )
    body = response.json()
    assert response.status_code == 200
    assert body["consistent"] and body["applied"]
    assert abs(sum(body["weights"].values()) - 1.0) < 1e-6

    refreshed = (
        await client.get(f"/api/v1/decisions/{decision['id']}", headers=admin_headers)
    ).json()
    assert abs(sum(criterion["weight"] for criterion in refreshed["criteria"]) - 1.0) < 1e-6


async def test_ahp_refuses_to_apply_inconsistent_judgements(
    client: AsyncClient, admin_headers, decision
) -> None:
    cyclic = [[1.0, 5.0, 0.2], [0.2, 1.0, 5.0], [5.0, 0.2, 1.0]]
    response = await client.post(
        f"/api/v1/decisions/{decision['id']}/weights/ahp",
        json={"matrix": cyclic, "apply_weights": True},
        headers=admin_headers,
    )
    assert response.status_code == 409


async def test_ahp_matrix_must_match_the_criterion_count(
    client: AsyncClient, admin_headers, decision
) -> None:
    response = await client.post(
        f"/api/v1/decisions/{decision['id']}/weights/ahp",
        json={"matrix": [[1.0, 2.0], [0.5, 1.0]]},
        headers=admin_headers,
    )
    assert response.status_code == 422
