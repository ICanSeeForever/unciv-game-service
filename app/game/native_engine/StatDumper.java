import com.unciv.UncivGame;
import com.unciv.models.metadata.GameSettings;
import com.unciv.models.ruleset.RulesetCache;
import com.unciv.logic.files.UncivFiles;
import com.unciv.logic.GameInfo;
import com.unciv.logic.civilization.Civilization;
import com.unciv.models.stats.Stats;
import java.nio.file.*;
import java.nio.charset.StandardCharsets;

/**
 * Headless stat dumper: loads a save with the native Unciv engine and prints, as a
 * single JSON line prefixed with "STATS_JSON=", each civ's per-turn income + net
 * happiness exactly as the game's world-screen top bar computes them.
 *
 * Bootstraps like Unciv's --creategame console path (no display): UncivGame(true) +
 * RulesetCache.loadRulesets(consoleMode). Run with cwd containing jsons/ (extracted
 * from Unciv.jar) and mods/<baseRuleset>/. Compiled against + run on Unciv.jar.
 */
public class StatDumper {
    public static void main(String[] args) throws Exception {
        UncivGame game = new UncivGame(true);
        UncivGame.Companion.setCurrent(game);
        game.setSettings(new GameSettings());
        RulesetCache.INSTANCE.loadRulesets(true, false);

        String save = new String(Files.readAllBytes(Paths.get(args[0])), StandardCharsets.UTF_8);
        GameInfo gi = UncivFiles.Companion.gameInfoFromString(save);

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
                  .append("\"happiness\":").append(civ.getHappiness())
                  .append("}");
            } catch (Throwable t) {
                // Skip civs the engine can't compute (leave them to the caller's fallback).
            }
        }
        sb.append("}");
        System.out.println(sb.toString());
    }

    private static String esc(String s) {
        return s.replace("\\", "\\\\").replace("\"", "\\\"");
    }
}
