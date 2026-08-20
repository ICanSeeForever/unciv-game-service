import com.unciv.UncivGame;
import com.unciv.models.metadata.GameSettings;
import com.unciv.models.ruleset.RulesetCache;
import com.unciv.logic.files.UncivFiles;
import com.unciv.logic.GameInfo;
import com.unciv.logic.civilization.Civilization;
import com.unciv.models.stats.Stats;
import java.io.BufferedReader;
import java.io.FileDescriptor;
import java.io.FileOutputStream;
import java.io.InputStreamReader;
import java.io.PrintStream;
import java.nio.file.*;
import java.nio.charset.StandardCharsets;

/**
 * Warm headless stat engine. Same math as StatDumper, but the ruleset + JVM stay
 * loaded across many saves so JIT can warm up: a single cold call is ~5s, every
 * subsequent one ~1s (vs. ~6s per fresh StatDumper process).
 *
 * Protocol (line-based over stdin/stdout, driven by native_stats.py):
 *   - On startup, after the ruleset loads, prints "DAEMON_READY".
 *   - For each line of stdin = absolute path to a save file, prints exactly one line:
 *       "STATS_JSON={...}"   on success, or
 *       "STATS_ERROR=<msg>"  if that one save fails (the daemon keeps running).
 *   - A blank line or "__QUIT__" shuts the daemon down.
 * A bad save never kills the process, so game-service can rely on the warm engine.
 */
public class StatDaemon {
    public static void main(String[] args) throws Exception {
        // Protocol channel: a raw autoflush stream straight onto fd 1. Unciv replaces
        // System.out with its own *buffered* stream (adds timestamps), whose flush()
        // doesn't reliably reach fd 1 while the JVM stays alive — so the parent would
        // never see our lines. Writing to FileDescriptor.out sidesteps that wrapper.
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

    private static String dump(GameInfo gi) {
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
        return sb.toString();
    }

    private static String esc(String s) {
        return s.replace("\\", "\\\\").replace("\"", "\\\"");
    }
}
