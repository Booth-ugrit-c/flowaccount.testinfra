using Sim.TestSupport;

namespace Sim.Service.Tests;

public class FacadeTests
{
    [Fact]
    public void Describe_carries_every_dependency_literal_from_source()
    {
        var expected = $"{Oracle.Name("Service")}({Oracle.Name("Core")},{Oracle.Name("Utils")},{Oracle.Name("Data")}+{Oracle.Name("Core")})";
        Assert.Equal(expected, Facade.Describe());
    }
}
