"""Gestion des tokens d'accès en lecture seule au flux iCal.

Chaque token est un identifiant long (32+ caractères) servant d'authentification
par URL pour les clients qui peuvent consommer un flux iCal via HTTP(S) :
Google Agenda, Apple Calendar, Thunderbird, DAVx5 (read-only), etc.

Persistance : ``output_dir / ical_tokens.json``.
"""

from __future__ import annotations

import json
import logging
import secrets
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


DEFAULT_TOKENS_PATH = "ical_tokens.json"
TOKEN_BYTES = 32  # → 64 caractères hex


def _now_iso() -> str:
    return datetime.now().isoformat()


def _new_token() -> str:
    """Génère un token hex de 32 octets (256 bits) — non devinable."""
    return secrets.token_hex(TOKEN_BYTES)


@dataclass(slots=True)
class ICalToken:
    """Un token d'abonnement iCal."""

    id: int
    label: str
    token: str
    created_at: str = field(default_factory=_now_iso)
    last_used_at: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


class ICalTokenStore:
    """Liste persistée de tokens (lecture / écriture atomique)."""

    def __init__(self, tokens: list[ICalToken] | None = None) -> None:
        self.tokens: list[ICalToken] = list(tokens or [])
        self._next_id = (max((t.id for t in self.tokens), default=0) + 1)

    # -- sérialisation --------------------------------------------------------

    def to_dict_list(self) -> list[dict]:
        return [t.to_dict() for t in self.tokens]

    @classmethod
    def from_dict_list(cls, payload: list[dict]) -> "ICalTokenStore":
        tokens: list[ICalToken] = []
        for raw in payload:
            tokens.append(
                ICalToken(
                    id=int(raw["id"]),
                    label=str(raw["label"]),
                    token=str(raw["token"]),
                    created_at=str(raw.get("created_at") or _now_iso()),
                    last_used_at=raw.get("last_used_at"),
                )
            )
        return cls(tokens)

    # -- opérations -----------------------------------------------------------

    def create(self, label: str) -> ICalToken:
        """Crée un nouveau token. Le token est généré aléatoirement."""
        tok = ICalToken(
            id=self._next_id,
            label=label.strip() or f"Token {self._next_id}",
            token=_new_token(),
        )
        self._next_id += 1
        self.tokens.append(tok)
        logger.info(
            "Nouveau token iCal créé (id=%d, label=%s, len=%d)",
            tok.id, tok.label, len(tok.token),
        )
        return tok

    def revoke(self, token_id: int) -> bool:
        """Révoque (supprime) un token par son id."""
        before = len(self.tokens)
        self.tokens = [t for t in self.tokens if t.id != token_id]
        return len(self.tokens) < before

    def find(self, token: str) -> ICalToken | None:
        """Recherche un token (lookup exact)."""
        for t in self.tokens:
            if t.token == token:
                return t
        return None

    def touch(self, token: ICalToken) -> None:
        """Mémorise la dernière utilisation."""
        token.last_used_at = _now_iso()

    # -- E/S ------------------------------------------------------------------

    def save(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Écriture atomique (write → rename) pour éviter les corruptions
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(self.to_dict_list(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(path)
        logger.info("Magasin de tokens iCal écrit : %s (%d entrées)", path, len(self.tokens))
        return path

    @classmethod
    def load(cls, path: Path) -> "ICalTokenStore":
        if not path.exists():
            return cls([])
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"ical_tokens.json corrompu : {exc}") from exc
        return cls.from_dict_list(payload)


def default_tokens_path(output_dir: Path) -> Path:
    return Path(output_dir) / DEFAULT_TOKENS_PATH
