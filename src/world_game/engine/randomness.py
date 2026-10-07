import hashlib
from typing import Literal, Protocol

from world_game.domain.common import GameError
from world_game.domain.fate import Draw, RngState


class DiceSource(Protocol):
    def roll(
        self,
        rng: RngState,
        check_id: str,
        side: Literal["actor", "defender"],
        reroll_index: int = 0,
    ) -> tuple[Draw, RngState]: ...


class CounterDice:
    """v0.1 policy: portable SHA-256 counter with unbiased rejection sampling."""

    def roll(
        self,
        rng: RngState,
        check_id: str,
        side: Literal["actor", "defender"],
        reroll_index: int = 0,
    ) -> tuple[Draw, RngState]:
        next_rng = rng.model_copy(deep=True)
        dice: list[int] = []
        while len(dice) < 4:
            if next_rng.counter >= 2**64:
                raise GameError("Dice counter exhausted; campaign cannot draw further dice.")
            raw = hashlib.sha256(
                b"world-game/dice/v1\0"
                + bytes.fromhex(rng.seed)
                + next_rng.counter.to_bytes(8, "big")
            ).digest()
            next_rng.counter += 1
            n = int.from_bytes(raw[:8], "big")
            if n < 2**64 - (2**64 % 3):
                dice.append(n % 3 - 1)
        return Draw(
            check_id=check_id,
            side=side,
            reroll_index=reroll_index,
            seed_ref=hashlib.sha256(bytes.fromhex(rng.seed)).hexdigest(),
            before_counter=rng.counter,
            after_counter=next_rng.counter,
            dice=dice,
        ), next_rng


class ScriptedDice:
    """Explicit test dice indexed by the serialized counter, surviving reload."""

    def __init__(self, rolls: list[list[int]]) -> None:
        self.rolls = rolls

    def roll(
        self,
        rng: RngState,
        check_id: str,
        side: Literal["actor", "defender"],
        reroll_index: int = 0,
    ) -> tuple[Draw, RngState]:
        index = rng.counter // 4
        if index >= len(self.rolls):
            raise GameError("Scripted dice exhausted.")
        after = rng.model_copy(update={"counter": rng.counter + 4})
        return Draw(
            check_id=check_id,
            side=side,
            reroll_index=reroll_index,
            seed_ref=rng.seed,
            before_counter=rng.counter,
            after_counter=after.counter,
            dice=self.rolls[index],
        ), after
