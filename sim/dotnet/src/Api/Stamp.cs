namespace Sim.Api;

public static class Stamp
{
    public static string Name() => "api-v1";
}

public static class Endpoint
{
    public static string Describe() => $"{Stamp.Name()}>{Sim.Service.Facade.Describe()}";
}
