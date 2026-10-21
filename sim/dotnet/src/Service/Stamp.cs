namespace Sim.Service;

public static class Stamp
{
    public static string Name() => "service-v1";
}

public static class Facade
{
    public static string Describe() =>
        $"{Stamp.Name()}({Sim.Core.Stamp.Name()},{Sim.Utils.Stamp.Name()},{Sim.Data.Store.Describe()})";
}
