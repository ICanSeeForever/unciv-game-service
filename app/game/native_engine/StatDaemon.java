import com.unciv.UncivGame;
import com.unciv.models.metadata.GameSettings;
import com.unciv.models.ruleset.RulesetCache;
import com.unciv.models.ruleset.tile.ResourceType;
import com.unciv.models.ruleset.tile.TileResource;
import com.unciv.logic.files.UncivFiles;
import com.unciv.logic.GameInfo;
import com.unciv.logic.civilization.Civilization;
import com.unciv.logic.city.City;
import com.unciv.logic.battle.CityCombatant;
import com.unciv.models.stats.Stats;
import java.io.BufferedReader;
import java.io.FileDescriptor;
import java.io.FileOutputStream;
import java.io.InputStreamReader;
import java.io.PrintStream;
import java.nio.file.*;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;

/**
 * Warm headless stat engine. Loads the ruleset + JVM once and stays resident so JIT
 * warms up (cold call ~5s, warm ~1.6s). For each save path on stdin it prints one
 * "STATS_JSON=" line with, per civ: per-turn income + happiness (the world-screen top
 * bar), turns to next social policy, net strategic-resource amounts, and per-city
 * growth/starvation/production turns + defensive strength — all straight from the
 * native engine, so the numbers match the game exactly.
 *
 * Protocol (line-based over a raw fd-1 stream so Unciv's buffered System.out wrapper
 * can't swallow it): "DAEMON_READY" once ready; then "STATS_JSON={...}" or
 * "STATS_ERROR=<msg>" per input line; a blank line / "__QUIT__" shuts it down.
 */
public class StatDaemon {
    private static List<String> strategicResources = null;  // ruleset is loaded once

    // Rankings screen columns (Unciv RankingType), in display order.
    private static final String[] RANKING_TYPES = {
        "Score", "Population", "Growth", "Production", "Gold",
        "Territory", "Force", "Happiness", "Technologies", "Culture",
    };

    public static void main(String[] args) throws Exception {
        // Protocol channel: raw autoflush stream on fd 1 (Unciv replaces System.out
        // with a buffered wrapper whose flush() doesn't reliably reach fd 1 while the
        // JVM stays alive, so plain println would never reach the parent).
        PrintStream out = new PrintStream(new FileOutputStream(FileDescriptor.out), true, "UTF-8");

        UncivGame game = new UncivGame(true);
        UncivGame.Companion.setCurrent(game);
        game.setSettings(new GameSettings());
        RulesetCache.INSTANCE.loadRulesets(true, false);

        out.println("DAEMON_READY");

        BufferedReader in = new BufferedReader(new InputStreamReader(System.in, StandardCharsets.UTF_8));
        String path;
        while ((path = in.readLine()) != null) {
            path = path.trim();
            if (path.isEmpty() || path.equals("__QUIT__")) break;
            try {
                String save = new String(Files.readAllBytes(Paths.get(path)), StandardCharsets.UTF_8);
                GameInfo gi = UncivFiles.Companion.gameInfoFromString(save);
                out.println(dump(gi));
            } catch (Throwable t) {
                out.println("STATS_ERROR=" + String.valueOf(t.getMessage()).replace("\n", " "));
            }
        }
        System.exit(0);
    }

    private static List<String> getStrategicResources(GameInfo gi) {
        if (strategicResources == null) {
            List<String> names = new ArrayList<>();
            for (TileResource r : gi.getRuleset().getTileResources().values())
                // Real map resources only: RekMOD also has Strategic-typed bookkeeping
                // "resources" (Policies, Factories, Ideology, Great Works, Trade Route)
                // with no terrain — those aren't the Horses/Iron/... bar the UI wants.
                if (r.getResourceType() == ResourceType.Strategic
                        && !r.getTerrainsCanBeFoundOn().isEmpty())
                    names.add(r.getName());
            strategicResources = names;
        }
        return strategicResources;
    }

    private static String dump(GameInfo gi) {
        List<String> strategic = getStrategicResources(gi);
        StringBuilder sb = new StringBuilder("STATS_JSON={");
        boolean first = true;
        for (Civilization civ : gi.getCivilizations()) {
            String name = civ.getCivName();
            try {
                if (civ.isBarbarian() || civ.isSpectator()) continue;
                civ.updateStatsForNextTurn();
                Stats s = civ.getStats().getStatsForNextTurn();

                if (!first) sb.append(",");
                first = false;
                sb.append("\"").append(esc(name)).append("\":{")
                  .append("\"gold\":").append(Math.round(s.getGold())).append(",")
                  .append("\"science\":").append(Math.round(s.getScience())).append(",")
                  .append("\"culture\":").append(Math.round(s.getCulture())).append(",")
                  .append("\"faith\":").append(Math.round(s.getFaith())).append(",")
                  .append("\"happiness\":").append(civ.getHappiness()).append(",")
                  .append("\"policyTurns\":").append(policyTurns(civ, s.getCulture())).append(",");

                // Net available strategic resources (produced - consumed + traded).
                sb.append("\"resources\":{");
                boolean rf = true;
                for (String r : strategic) {
                    if (!rf) sb.append(",");
                    rf = false;
                    sb.append("\"").append(esc(r)).append("\":").append(civ.getResourceAmount(r));
                }
                sb.append("},");

                // Per-city plate numbers, keyed by "x,y" (matches the save's location).
                sb.append("\"cities\":{");
                boolean cf = true;
                for (City c : civ.getCities()) {
                    if (!cf) sb.append(",");
                    cf = false;
                    appendCity(sb, c);
                }
                sb.append("},");

                // Turns to research each not-yet-researched tech (native turnsToTech,
                // which accounts for already-accumulated research + cost modifiers).
                sb.append("\"techTurns\":{");
                boolean tf = true;
                for (String tech : gi.getRuleset().getTechnologies().keySet()) {
                    if (civ.getTech().isResearched(tech)) continue;
                    if (!tf) sb.append(",");
                    tf = false;
                    sb.append("\"").append(esc(tech)).append("\":")
                      .append(parseTurns(civ.getTech().turnsToTech(tech)));
                }
                sb.append("},");

                // Owner era (drives era-variant improvement/city/embark textures).
                sb.append("\"era\":\"").append(esc(civ.getEra().getName())).append("\",");

                // Rankings screen: each stat's value (Unciv getStatForRanking), alive
                // flag (defeated civs show at the bottom with no value), and major flag
                // (the rankings screen lists only major civs, not city-states).
                sb.append("\"major\":").append(civ.isMajorCiv()).append(",");
                sb.append("\"alive\":").append(!civ.isDefeated()).append(",");
                sb.append("\"ranking\":{");
                boolean rkf = true;
                for (String rt : RANKING_TYPES) {
                    if (!rkf) sb.append(",");
                    rkf = false;
                    int rv;
                    try {
                        rv = civ.getStatForRanking(
                            com.unciv.ui.screens.victoryscreen.RankingType.valueOf(rt));
                    } catch (Throwable e) { rv = 0; }
                    sb.append("\"").append(rt).append("\":").append(rv);
                }
                sb.append("},");

                // Positions of this civ's embarked units (land units on water).
                sb.append("\"embarked\":[");
                boolean ef = true;
                java.util.Iterator<com.unciv.logic.map.mapunit.MapUnit> uit =
                    civ.getUnits().getCivUnits().iterator();
                while (uit.hasNext()) {
                    com.unciv.logic.map.mapunit.MapUnit u = uit.next();
                    if (!u.isEmbarked()) continue;
                    if (!ef) sb.append(",");
                    ef = false;
                    sb.append("\"").append(u.getTile().getPosition().getX())
                      .append(",").append(u.getTile().getPosition().getY()).append("\"");
                }
                sb.append("]}");
            } catch (Throwable t) {
                // Leave this civ out; the caller falls back for it.
            }
        }

        // Per-tile real yield for owned tiles: the exact stats the owning city gets
        // from the tile (base + city/policy/religion/wonder bonuses), keyed by "x,y".
        // Neutral tiles are omitted (the frontend shows their base yield).
        if (!first) sb.append(",");
        sb.append("\"__tileYields__\":{");
        boolean tyf = true;
        for (com.unciv.logic.map.tile.Tile t : gi.getTileMap().getTileList()) {
            City oc = t.getOwningCity();
            if (oc == null) continue;
            Stats s;
            try { s = t.getStats().getTileStats(oc, oc.getCiv()); }
            catch (Throwable e) { continue; }
            if (!tyf) sb.append(",");
            tyf = false;
            sb.append("\"").append(t.getPosition().getX()).append(",")
              .append(t.getPosition().getY()).append("\":{")
              .append("\"food\":").append(Math.round(s.getFood())).append(",")
              .append("\"production\":").append(Math.round(s.getProduction())).append(",")
              .append("\"gold\":").append(Math.round(s.getGold())).append(",")
              .append("\"science\":").append(Math.round(s.getScience())).append(",")
              .append("\"culture\":").append(Math.round(s.getCulture())).append(",")
              .append("\"faith\":").append(Math.round(s.getFaith())).append("}");
        }
        sb.append("}");

        sb.append("}");
        return sb.toString();
    }

    private static void appendCity(StringBuilder sb, City c) {
        int x = Math.round(c.getLocation().getX());
        int y = Math.round(c.getLocation().getY());
        Integer grow = c.getPopulation().getNumTurnsToNewPopulation();
        Integer starve = c.getPopulation().getNumTurnsToStarvation();
        int production = -1;
        String cur = c.getCityConstructions().currentConstructionName();
        if (cur != null && !cur.isEmpty()) {
            try { production = c.getCityConstructions().turnsToConstruction(cur, true); }
            catch (Throwable t) { production = -1; }
        }
        int strength;
        try { strength = new CityCombatant(c).getDefendingStrength(null); }
        catch (Throwable t) { strength = -1; }
        // Current / max HP (max varies with buildings & era) — the viewer draws a
        // health bar only when health < maxHealth.
        int health = -1, maxHealth = -1;
        try { health = c.getHealth(); } catch (Throwable t) { health = -1; }
        try { maxHealth = c.getMaxHealth(); } catch (Throwable t) { maxHealth = -1; }
        sb.append("\"").append(x).append(",").append(y).append("\":{")
          .append("\"growth\":").append(grow == null ? -1 : grow).append(",")
          .append("\"starve\":").append(starve == null ? -1 : starve).append(",")
          .append("\"production\":").append(production).append(",")
          .append("\"strength\":").append(strength).append(",")
          .append("\"health\":").append(health).append(",")
          .append("\"maxHealth\":").append(maxHealth)
          .append("}");
    }

    /** Leading integer of a turnsToTech() string ("2", "∞", …); -1 if none. */
    private static int parseTurns(String s) {
        if (s == null) return -1;
        StringBuilder d = new StringBuilder();
        for (int i = 0; i < s.length(); i++) {
            char ch = s.charAt(i);
            if (ch >= '0' && ch <= '9') d.append(ch);
            else if (d.length() > 0) break;
        }
        if (d.length() == 0) return -1;  // "∞" or non-numeric
        try { return Integer.parseInt(d.toString()); } catch (Exception e) { return -1; }
    }

    /** Turns until the next social policy: -1 = ready now ("!"), -2 = no culture income. */
    private static int policyTurns(Civilization civ, float culturePerTurn) {
        int stored = civ.getPolicies().getStoredCulture();
        int needed = civ.getPolicies().getCultureNeededForNextPolicy();
        if (stored >= needed) return -1;
        if (culturePerTurn <= 0) return -2;
        return (int) Math.ceil((needed - stored) / culturePerTurn);
    }

    private static String esc(String s) {
        return s.replace("\\", "\\\\").replace("\"", "\\\"");
    }
}
