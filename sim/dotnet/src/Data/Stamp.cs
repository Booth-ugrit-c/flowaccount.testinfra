namespace Sim.Data;

public static class Stamp
{
    public static string Name() => "data-v1";
}

public static class Store
{
    public static string Describe() => $"{Stamp.Name()}+{Sim.Core.Stamp.Name()}";
}
