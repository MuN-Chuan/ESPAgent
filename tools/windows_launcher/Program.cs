using System;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Windows.Forms;

[assembly: AssemblyTitle("ESPAgent")]
[assembly: AssemblyDescription("ESPAgent desktop application launcher")]
[assembly: AssemblyCompany("ESPAgent")]
[assembly: AssemblyProduct("ESPAgent")]
[assembly: AssemblyVersion("1.0.0.0")]
[assembly: AssemblyFileVersion("1.0.0.0")]

internal static class Program
{
    [STAThread]
    private static int Main()
    {
        string root = FindApplicationRoot(AppDomain.CurrentDomain.BaseDirectory);
        if (root == null)
        {
            ShowError(
                "ESPAgent could not find start_espagent_app.cmd.\n\n" +
                "Keep ESPAgent.exe in the project folder or one of its direct subfolders."
            );
            return 2;
        }

        string launcher = Path.Combine(root, "start_espagent_app.cmd");
        string project = Path.Combine(root, "apps", "demo_host", "pyproject.toml");
        if (!File.Exists(project))
        {
            ShowError(
                "The ESPAgent installation is incomplete.\n\n" +
                "Missing: apps\\demo_host\\pyproject.toml\n\n" +
                "Copy the complete project directory, not only ESPAgent.exe."
            );
            return 3;
        }

        try
        {
            ProcessStartInfo startInfo = new ProcessStartInfo
            {
                FileName = Environment.GetEnvironmentVariable("COMSPEC") ?? "cmd.exe",
                Arguments = BuildArguments(launcher),
                WorkingDirectory = root,
                UseShellExecute = false,
                CreateNoWindow = false,
                WindowStyle = ProcessWindowStyle.Normal,
            };

            Process process = Process.Start(startInfo);
            if (process == null)
            {
                throw new InvalidOperationException("Windows did not create the launcher process.");
            }
            return 0;
        }
        catch (Exception exception)
        {
            ShowError(
                "ESPAgent failed to start.\n\n" +
                exception.Message
            );
            return 1;
        }
    }

    private static string FindApplicationRoot(string startDirectory)
    {
        DirectoryInfo directory = new DirectoryInfo(startDirectory);
        for (int depth = 0; directory != null && depth < 5; depth++, directory = directory.Parent)
        {
            if (File.Exists(Path.Combine(directory.FullName, "start_espagent_app.cmd")) &&
                File.Exists(Path.Combine(directory.FullName, "start_espagent_app.py")))
            {
                return directory.FullName;
            }
        }
        return null;
    }

    private static string BuildArguments(string launcher)
    {
        return string.Format(
            "/d /s /c \"\"{0}\"\"",
            launcher
        );
    }

    private static void ShowError(string message)
    {
        MessageBox.Show(message, "ESPAgent", MessageBoxButtons.OK, MessageBoxIcon.Error);
    }
}
