using System;
using System.Globalization;
using System.IO;
using System.Text;
using UnityModManagerNet;
using UnityEngine;
using UnityEngine.SceneManagement;

namespace ADOFAIGOBridge
{
    /// <summary>
    /// Minimal command bridge used by the ADOFAI GO desktop app.
    /// The app drops request.txt next to this DLL; this mod opens the chart
    /// through ADOFAI's own scnEditor entry point.
    /// </summary>
    public static class Main
    {
        public const string Version = "0.2.1";

        private const string RequestName = "request.txt";
        private const string StatusName = "status.txt";
        private const string HeartbeatName = "heartbeat.txt";
        private const string LogName = "bridge.log";

        /// <summary>Scenes that are fully up and safe to leave for scnEditor.</summary>
        private static readonly string[] StableScenes =
        {
            "scnLevelSelect",
            "scnGame",
            "scnCalibration",
            "scnCLS",
            "scnMinesweeper",
        };

        // Give a stable scene a moment to finish its own setup before we swap it.
        private const float StableSceneSettleSeconds = 0.75f;
        // Empty scnEditor lets its own Start consume levelToOpenOnLoad.
        private const float EditorInitSeconds = 1.5f;
        // Last resort for booting or unknown scenes (matches the app's wait limit).
        private const float PendingFallbackSeconds = 20f;
        private const float SceneSettleCeiling = 600f;

        private static string _dir;
        private static float _pollTimer;
        private static float _heartbeatTimer;
        private static string _pendingId;
        private static string _pendingPath;
        private static float _pendingWait;
        private static string _lastScene;
        private static float _sceneSettle;
        private static bool _sceneErrorLogged;

        public static bool Load(UnityModManager.ModEntry modEntry)
        {
            _dir = modEntry.Path;
            modEntry.OnUpdate = OnUpdate;
            Log("loaded version " + Version + " dir=" + _dir);
            WriteHeartbeat("loaded");
            return true;
        }

        private static void OnUpdate(UnityModManager.ModEntry modEntry, float dt)
        {
            // The editor is often paused, where Time.deltaTime is zero.
            float delta = Time.unscaledDeltaTime;
            _pollTimer += delta;
            _heartbeatTimer += delta;
            _sceneSettle += delta;
            if (_sceneSettle > SceneSettleCeiling)
            {
                _sceneSettle = SceneSettleCeiling;
            }

            if (_heartbeatTimer >= 1f)
            {
                _heartbeatTimer = 0f;
                WriteHeartbeat(_pendingPath == null ? "idle" : "pending");
            }

            if (_pendingPath != null)
            {
                _pendingWait += delta;
                TryOpenPending();
                return;
            }

            if (_pollTimer < 0.25f)
            {
                return;
            }
            _pollTimer = 0f;
            // Keeps bridge.log's "scene changed" line current even when idle.
            RefreshScene();
            TryReadRequest();
        }

        private static void TryReadRequest()
        {
            string requestPath = Path.Combine(_dir, RequestName);
            if (!File.Exists(requestPath))
            {
                return;
            }

            string text;
            try
            {
                text = File.ReadAllText(requestPath, Encoding.UTF8);
            }
            catch (Exception exc)
            {
                Log("read request failed: " + exc.Message);
                return;
            }

            try
            {
                File.Delete(requestPath);
            }
            catch (Exception exc)
            {
                Log("delete request failed: " + exc.Message);
            }

            string id = "";
            string path = "";
            string[] lines = text.Replace("\r", "").Split('\n');
            for (int i = 0; i < lines.Length; i++)
            {
                string line = lines[i].Trim();
                int split = line.IndexOf('=');
                if (split <= 0)
                {
                    continue;
                }
                string key = line.Substring(0, split).Trim().ToLowerInvariant();
                string value = line.Substring(split + 1).Trim();
                if (key == "id")
                {
                    id = value;
                }
                else if (key == "path")
                {
                    path = value;
                }
            }

            if (string.IsNullOrEmpty(id) || string.IsNullOrEmpty(path))
            {
                WriteStatus(id, false, "请求缺少 id 或 path");
                return;
            }

            string resolved = ResolveLevelPath(path);
            if (resolved == null)
            {
                WriteStatus(id, false, "谱面文件不存在或不是 .adofai：" + path);
                return;
            }

            _pendingId = id;
            _pendingPath = resolved;
            _pendingWait = 0f;
            Log("request " + id + " accepted: " + resolved);
            TryOpenPending();
        }

        private static void TryOpenPending()
        {
            if (_pendingPath == null)
            {
                return;
            }

            string scene = RefreshScene();

            if (scene != "scnEditor")
            {
                if (IsStableScene(scene))
                {
                    // The game is sitting in a fully loaded menu/level scene: reload
                    // into scnEditor as soon as that scene has settled, instead of
                    // waiting out a fixed timeout.
                    if (_sceneSettle < StableSceneSettleSeconds)
                    {
                        return;
                    }
                    OpenBySceneReload(_pendingId, _pendingPath, scene);
                    return;
                }

                // Booting game (empty scene, scnSplash, scnLoading, ...) or a scene
                // we do not know yet: wait for a stable scene to show up. The fixed
                // timeout only covers a game that never gets there.
                if (_pendingWait < PendingFallbackSeconds)
                {
                    return;
                }
                OpenBySceneReload(_pendingId, _pendingPath, scene);
                return;
            }

            scnEditor editor = scnEditor.instance;
            if (editor != null && editor.customLevel != null &&
                editor.customLevel.levelMaker != null)
            {
                try
                {
                    editor.OpenLevel(_pendingPath);
                    WriteStatus(_pendingId, true, "已在编辑器中打开");
                    Log("opened in current editor: " + _pendingPath);
                    ClearPending();
                }
                catch (Exception exc)
                {
                    Log("editor.OpenLevel failed: " + exc);
                    OpenBySceneReload(_pendingId, _pendingPath, scene);
                }
                return;
            }

            // Empty editor or scene still initializing: let scnEditor consume
            // the path during its own Start.
            if (_pendingWait < EditorInitSeconds)
            {
                return;
            }
            OpenBySceneReload(_pendingId, _pendingPath, scene);
        }

        private static string RefreshScene()
        {
            string scene = "";
            try
            {
                scene = SceneManager.GetActiveScene().name ?? "";
            }
            catch (Exception exc)
            {
                if (!_sceneErrorLogged)
                {
                    _sceneErrorLogged = true;
                    Log("get scene failed: " + exc.Message);
                }
            }

            if (scene != _lastScene)
            {
                _lastScene = scene;
                _sceneSettle = 0f;
                Log("scene changed: " + (scene.Length == 0 ? "(none)" : scene));
            }
            return scene;
        }

        private static bool IsStableScene(string scene)
        {
            for (int i = 0; i < StableScenes.Length; i++)
            {
                if (StableScenes[i] == scene)
                {
                    return true;
                }
            }
            return false;
        }

        private static void OpenBySceneReload(string id, string path, string scene)
        {
            try
            {
                scnEditor.levelToOpenOnLoad = path;
                WriteStatus(id, true, "已请求编辑器载入");
                Log("reloading scnEditor from " + scene + " for " + path);
                ClearPending();
                SceneManager.LoadScene("scnEditor");
            }
            catch (Exception exc)
            {
                Log("scene reload failed: " + exc);
                WriteStatus(id, false, "载入失败：" + exc.Message);
                ClearPending();
            }
        }

        private static void ClearPending()
        {
            _pendingId = null;
            _pendingPath = null;
            _pendingWait = 0f;
        }

        private static string ResolveLevelPath(string raw)
        {
            string path = raw.Trim().Trim('"');
            if (File.Exists(path) &&
                path.EndsWith(".adofai", StringComparison.OrdinalIgnoreCase))
            {
                return path;
            }
            if (!Directory.Exists(path))
            {
                return null;
            }

            string main = Path.Combine(path, "main.adofai");
            if (File.Exists(main))
            {
                return main;
            }
            string[] files = Directory.GetFiles(path, "*.adofai");
            if (files.Length > 0)
            {
                Array.Sort(files, StringComparer.OrdinalIgnoreCase);
                return files[0];
            }
            return null;
        }

        private static void WriteStatus(string id, bool ok, string message)
        {
            try
            {
                StringBuilder text = new StringBuilder();
                text.Append("id=").Append(id ?? "").Append('\n');
                text.Append("ok=").Append(ok ? "1" : "0").Append('\n');
                text.Append("message=").Append(message ?? "").Append('\n');
                text.Append("time=")
                    .Append(DateTimeOffset.UtcNow.ToUnixTimeSeconds())
                    .Append('\n');
                WriteAtomic(Path.Combine(_dir, StatusName), text.ToString());
            }
            catch (Exception exc)
            {
                Log("write status failed: " + exc.Message);
            }
        }

        private static void WriteHeartbeat(string state)
        {
            try
            {
                StringBuilder text = new StringBuilder();
                text.Append("version=").Append(Version).Append('\n');
                text.Append("state=").Append(state ?? "").Append('\n');
                text.Append("time=")
                    .Append(DateTimeOffset.UtcNow.ToUnixTimeSeconds())
                    .Append('\n');
                WriteAtomic(Path.Combine(_dir, HeartbeatName), text.ToString());
            }
            catch (Exception exc)
            {
                Log("write heartbeat failed: " + exc.Message);
            }
        }

        private static void WriteAtomic(string path, string text)
        {
            string temp = path + ".tmp";
            File.WriteAllText(temp, text, new UTF8Encoding(false));
            if (File.Exists(path))
            {
                File.Delete(path);
            }
            File.Move(temp, path);
        }

        private static void Log(string message)
        {
            try
            {
                string line = DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss", CultureInfo.InvariantCulture)
                    + " " + message + "\r\n";
                File.AppendAllText(Path.Combine(_dir, LogName), line, new UTF8Encoding(false));
            }
            catch
            {
                // Never let diagnostics break chart loading.
            }
        }
    }
}
