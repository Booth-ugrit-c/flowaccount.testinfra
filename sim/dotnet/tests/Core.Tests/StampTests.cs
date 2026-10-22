using Sim.TestSupport;

namespace Sim.Core.Tests;

public class StampTests
{
    [Fact]
    public void Name_matches_the_source_literal()
    {
        Assert.Equal(Oracle.Name("Core"), Stamp.Name());
    }
}
