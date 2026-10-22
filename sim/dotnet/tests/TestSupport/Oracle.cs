using System.Text.RegularExpressions;

namespace Sim.TestSupport;

public static class Oracle
{
    public static string Name(string project)
    {
        var path = Path.Combine(Root(), "src", project, "Stamp.cs");
        var match = Regex.Match(File.ReadAllText(path), @"Name\(\) => ""([^""]+)""");
        if (!match.Success)
        {
            throw new InvalidOperationException($"no Name() literal in {path}");
        }
        return match.Groups[1].Value;
    }

    private static string Root()
    {
        var dir = new DirectoryInfo(AppContext.BaseDirectory);
        while (dir is not null && !File.Exists(Path.Combine(dir.FullName, "global.json")))
        {
            dir = dir.Parent;
        }
        return dir?.FullName ?? throw new InvalidOperationException("global.json not found above the test binary");
    }
}
