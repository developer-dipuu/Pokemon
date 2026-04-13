Editable RPG data files live here.

- `regions.json`: region ids and labels.
- `starters.json`: the three selectable starters for each region.
- `generator_defaults.json`: starter and generator defaults.
- `catch_settings.json`: ball multipliers and future catch-tuning knobs.
- `encounter_pools.json`: normal `/hunt` encounter tables by region.
- `safari_pools.json`: `/safari` encounter tables by region, now generated from the imported safari source list.
- `species_catch_rates.json`: per-species catch rates with a global default fallback, now generated from the imported catch-rate source.
- `rarity_weights.json`: optional multiplier table for encounter rarity weighting, now generated from the imported rarity source.
- `imported/`: copied reference data used by the stats pages and future systems.

Imported reference files currently used:

- `imported/species_reference.json`: artwork, types, and EV yield.
- `imported/growth_data.json`: growth rate and base EXP.
- `imported/base_stats.json`: base stat table for the `/stats` numeric page.
- `imported/move_info.json`: move power, accuracy, type, and category.
- `imported/exp_chart.json`: EXP curves.
- `imported/evolution_chains.json`: evolution display data.
- `imported/shiny_art.json`: shiny artwork URLs.

Imported reference files kept for later systems:

- `imported/pokedex_regions.json`
- `imported/tms.json`
- `imported/tm_prices.json`
- `imported/stones.json`
- `imported/region_locations.json`

The normal `/hunt` table is still the smaller editable region pool we built locally. The safari, catch-rate, art, stat, EXP, and move detail data now come from your imported files.
