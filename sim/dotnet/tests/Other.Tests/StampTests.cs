using Sim.TestSupport;

namespace Sim.Other.Tests;

public class StampTests
{
    [Fact]
    public void Name_matches_the_source_literal()
    {
        Assert.Equal(Oracle.Name("Other"), Stamp.Name());
    }
}
