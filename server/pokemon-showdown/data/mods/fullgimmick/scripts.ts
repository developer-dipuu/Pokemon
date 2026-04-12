export const Scripts: ModdedBattleScriptsData = {
	gen: 9,
	inherit: 'gen9',
	runAction(action) {
		if (action.choice === 'runDynamax') {
			action.pokemon.addVolatile('dynamax');
			(action.pokemon.side as any).fullGimmickDynamaxUsed = true;
			action.pokemon.side.dynamaxUsed = true;
			if (action.pokemon.side.allySide) {
				(action.pokemon.side.allySide as any).fullGimmickDynamaxUsed = true;
				action.pokemon.side.allySide.dynamaxUsed = true;
			}
			return;
		}
		return Object.getPrototypeOf(this).runAction.call(this, action);
	},
	actions: {
		inherit: true,
		canTerastallize(pokemon) {
			if (this.dex.gen !== 9) {
				return null;
			}
			return pokemon.teraType;
		},
	},
	pokemon: {
		inherit: true,
		getDynamaxRequest(skipChecks?: boolean) {
			if (!skipChecks) {
				if (!this.side.canDynamaxNow()) return;
				if (
					this.species.isMega || this.species.isPrimal || this.species.forme === "Ultra"
				) {
					return;
				}
				if (this.species.cannotDynamax || this.illusion?.species.cannotDynamax) return;
			}
			const result: DynamaxOptions = {maxMoves: []};
			let atLeastOne = false;
			for (const moveSlot of this.moveSlots) {
				const move = this.battle.dex.moves.get(moveSlot.id);
				const maxMove = this.battle.actions.getMaxMove(move, this);
				if (maxMove) {
					if (this.maxMoveDisabled(move)) {
						result.maxMoves.push({move: maxMove.id, target: maxMove.target, disabled: true});
					} else {
						result.maxMoves.push({move: maxMove.id, target: maxMove.target});
						atLeastOne = true;
					}
				}
			}
			if (!atLeastOne) return;
			if (this.canGigantamax && this.gigantamax) result.gigantamax = this.canGigantamax;
			return result;
		},
	},
	side: {
		inherit: true,
		canDynamaxNow() {
			return !(this as any).fullGimmickDynamaxUsed;
		},
	},
};
