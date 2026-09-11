"""core/oidc.py

ADR-0031 (L2): OIDC provider 抽象 + claim → player principal 解決。

callback 後のアプリ内ロジックは provider 抽象（`OidcProvider`）だけに依存する。実 LINE/Google は
token 交換 + JWKS 検証を行う薄い実装（実環境タスク）、テスト/ローカルは `FakeOidcProvider`。
これで初回 link・principal 解決・player merge 連結の全経路を実 IdP なしでテストできる。

principal token（ADR-0027）の発行は API 層が行う。本モジュールは player_id の解決までを担う。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from core.player_repository import DuplicateDisplayNameError

if TYPE_CHECKING:
    from core.auth_identity_repository import AuthIdentityRepository
    from core.player_repository import PlayerRepository


class OidcError(Exception):
    """OIDC 検証失敗（code=invalid_idp_code）。"""


@dataclass
class VerifiedClaim:
    """IdP の認可コードを検証して得た最小クレーム（PII 最小化, ADR-0028 D6）。"""

    provider: str
    subject: str
    display_name_seed: str | None = None


class OidcProvider(Protocol):
    """認可コードを検証して `VerifiedClaim` を返す provider 抽象（ADR-0031 D1）。"""

    name: str

    def verify_code(self, code: str, *, nonce: str | None = None) -> VerifiedClaim:
        ...


class FakeOidcProvider:
    """テスト/ローカル用の provider。事前登録した code→claim を返す（実 HTTP なし）。"""

    def __init__(self, name: str = "fake") -> None:
        self.name = name
        self._codes: dict[str, VerifiedClaim] = {}

    def register(self, code: str, subject: str, display_name_seed: str | None = None) -> None:
        self._codes[code] = VerifiedClaim(self.name, subject, display_name_seed)

    def verify_code(self, code: str, *, nonce: str | None = None) -> VerifiedClaim:
        claim = self._codes.get(code)
        if claim is None:
            raise OidcError(f"認可コードが無効です（provider={self.name}）。")
        return claim


def _unique_display_name(player_repo: "PlayerRepository", seed: str | None) -> str:
    """registry の重複制約に収まる表示名を作る（IdP 名は衝突しうるためサフィックスで一意化）。"""
    base = (seed or "").strip() or "IdP ユーザー"
    taken = {p.display_name for p in player_repo.list_players(include_merged=True)}
    if base not in taken:
        return base
    n = 1
    while f"{base} ({n})" in taken:
        n += 1
    return f"{base} ({n})"


def resolve_player_for_claim(
    claim: VerifiedClaim,
    identity_repo: "AuthIdentityRepository",
    player_repo: "PlayerRepository",
) -> str:
    """claim を player_id に解決する（ADR-0031 D3）。

    - 既存 auth_identity があれば、その player_id を resolve_canonical（merge, ADR-0030）して返す。
    - 無ければ新規 player を内部採番（player_id は sub から導出しない, ADR-0004）+ link して返す。
      会場の既存 player との統合は後でスタッフが player merge（ADR-0030）で行う。
    """
    existing = identity_repo.get(claim.provider, claim.subject)
    if existing is not None:
        return player_repo.resolve_canonical(existing.player_id)
    # 初回 link: 新規 player を作って auth_identity を張る。
    name = _unique_display_name(player_repo, claim.display_name_seed)
    try:
        player = player_repo.create_player(name)
    except DuplicateDisplayNameError:
        player = player_repo.create_player(f"{name} ({claim.subject[:6]})")
    identity_repo.link(claim.provider, claim.subject, player.player_id,
                       display_name_seed=claim.display_name_seed)
    return player.player_id
