using System.Net.Sockets;
using System.Text;
using Sim.TestSupport;

namespace Sim.Api.IntegrationTests;

[Trait("Category", "integration")]
public class ServiceTests
{
    [Fact]
    public void Endpoint_chain_carries_every_literal_from_source()
    {
        var service = $"{Oracle.Name("Service")}({Oracle.Name("Core")},{Oracle.Name("Utils")},{Oracle.Name("Data")}+{Oracle.Name("Core")})";
        Assert.Equal($"{Oracle.Name("Api")}>{service}", Endpoint.Describe());
    }

    [Fact]
    public async Task Sql_server_answers_a_postgres_startup_with_an_authentication_request()
    {
        var reply = await Wire.Exchange(Wire.Endpoint("SIM_SQL", 5432), Wire.PostgresStartup(), 1);
        Assert.Equal((byte)'R', reply[0]);
    }

    [Fact]
    public async Task Cache_server_answers_ping_with_pong()
    {
        var reply = await Wire.Exchange(Wire.Endpoint("SIM_CACHE", 6379), Wire.RedisPing(), 7);
        Assert.Equal("+PONG\r\n", Encoding.ASCII.GetString(reply));
    }
}

public static class Wire
{
    private static readonly TimeSpan Limit = TimeSpan.FromSeconds(10);

    public static (string Host, int Port) Endpoint(string prefix, int defaultPort)
    {
        var host = Environment.GetEnvironmentVariable($"{prefix}_HOST");
        if (string.IsNullOrEmpty(host))
        {
            throw new InvalidOperationException($"{prefix}_HOST is not set");
        }
        var port = Environment.GetEnvironmentVariable($"{prefix}_PORT");
        return (host, string.IsNullOrEmpty(port) ? defaultPort : int.Parse(port));
    }

    public static async Task<byte[]> Exchange((string Host, int Port) endpoint, byte[] request, int replyLength)
    {
        using var timeout = new CancellationTokenSource(Limit);
        using var client = new TcpClient();
        await client.ConnectAsync(endpoint.Host, endpoint.Port, timeout.Token);
        var stream = client.GetStream();
        await stream.WriteAsync(request, timeout.Token);
        var reply = new byte[replyLength];
        await stream.ReadExactlyAsync(reply, timeout.Token);
        return reply;
    }

    public static byte[] PostgresStartup()
    {
        var parameters = Encoding.ASCII.GetBytes("user\0postgres\0database\0postgres\0\0");
        var message = new byte[8 + parameters.Length];
        WriteInt32(message, 0, message.Length);
        WriteInt32(message, 4, 196608);
        parameters.CopyTo(message, 8);
        return message;
    }

    public static byte[] RedisPing() => Encoding.ASCII.GetBytes("*1\r\n$4\r\nPING\r\n");

    private static void WriteInt32(byte[] target, int offset, int value)
    {
        target[offset] = (byte)(value >> 24);
        target[offset + 1] = (byte)(value >> 16);
        target[offset + 2] = (byte)(value >> 8);
        target[offset + 3] = (byte)value;
    }
}
