import asyncio
import binascii
import random
import socket
from unittest import TestCase
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization

from aioquic.asyncio.client import connect
from aioquic.asyncio.protocol import QuicConnectionProtocol
from aioquic.asyncio.server import serve
from aioquic.quic.configuration import QuicConfiguration
from aioquic.quic.logger import QuicLogger

from .utils import (
    SERVER_CACERTFILE,
    SERVER_CERTFILE,
    SERVER_KEYFILE,
    generate_ec_certificate,
    run,
)

real_sendto = socket.socket.sendto


def sendto_with_loss(self, data, addr=None):
    """
    Simulate 25% packet loss.
    """
    if random.random() > 0.25:
        real_sendto(self, data, addr)


class SessionTicketStore:
    def __init__(self):
        self.tickets = {}

    def add(self, ticket):
        self.tickets[ticket.ticket] = ticket

    def pop(self, label):
        return self.tickets.pop(label, None)


def handle_stream(reader, writer):
    async def serve():
        data = await reader.read()
        writer.write(bytes(reversed(data)))
        writer.write_eof()

    asyncio.ensure_future(serve())


class HighLevelTest(TestCase):
    def setUp(self):
        self.server = None

    def tearDown(self):
        if self.server is not None:
            self.server.close()

    async def run_client(
        self,
        host,
        port=4433,
        cadata=None,
        cafile=SERVER_CACERTFILE,
        configuration=None,
        request=b"ping",
        **kwargs
    ):
        if configuration is None:
            configuration = QuicConfiguration(is_client=True)
        configuration.load_verify_locations(cadata=cadata, cafile=cafile)
        async with connect(host, port, configuration=configuration, **kwargs) as client:
            # waiting for connected when connected returns immediately
            await client.wait_connected()

            reader, writer = await client.create_stream()
            self.assertEqual(writer.can_write_eof(), True)
            self.assertEqual(writer.get_extra_info("stream_id"), 0)

            writer.write(request)
            writer.write_eof()

            response = await reader.read()

        # waiting for closed when closed returns immediately
        await client.wait_closed()

        return response

    async def run_server(self, configuration=None, **kwargs):
        if configuration is None:
            configuration = QuicConfiguration(is_client=False)
            configuration.load_cert_chain(SERVER_CERTFILE, SERVER_KEYFILE)
        self.server = await serve(
            host="::",
            port="4433",
            configuration=configuration,
            stream_handler=handle_stream,
            **kwargs
        )
        return self.server

    def test_connect_and_serve(self):
        run(self.run_server())
        response = run(self.run_client("127.0.0.1"))
        self.assertEqual(response, b"gnip")

    def test_connect_and_serve_ec_certificate(self):
        certificate, private_key = generate_ec_certificate(common_name="localhost")

        run(
            self.run_server(
                configuration=QuicConfiguration(
                    certificate=certificate, private_key=private_key, is_client=False,
                )
            )
        )

        response = run(
            self.run_client(
                "127.0.0.1",
                cadata=certificate.public_bytes(serialization.Encoding.PEM),
                cafile=None,
            )
        )

        self.assertEqual(response, b"gnip")

    def test_connect_and_serve_large(self):
        """
        Transfer enough data to require raising MAX_DATA and MAX_STREAM_DATA.
        """
        data = b"Z" * 2097152
        run(self.run_server())
        response = run(self.run_client("127.0.0.1", request=data))
        self.assertEqual(response, data)

    def test_connect_and_serve_without_client_configuration(self):
        async def run_client_without_config(host, port=4433):
            async with connect(host, port) as client:
                await client.ping()

        run(self.run_server())
        with self.assertRaises(ConnectionError):
            run(run_client_without_config("127.0.0.1"))

    def test_connect_and_serve_writelines(self):
        async def run_client_writelines(host, port=4433):
            configuration = QuicConfiguration(is_client=True)
            configuration.load_verify_locations(cafile=SERVER_CACERTFILE)
            async with connect(host, port, configuration=configuration) as client:
                reader, writer = await client.create_stream()
                assert writer.can_write_eof() is True

                writer.writelines([b"01234567", b"89012345"])
                writer.write_eof()

                return await reader.read()

        run(self.run_server())
        response = run(run_client_writelines("127.0.0.1"))
        self.assertEqual(response, b"5432109876543210")

    @patch("socket.socket.sendto", new_callable=lambda: sendto_with_loss)
    def test_connect_and_serve_with_packet_loss(self, mock_sendto):
        """
        This test ensures handshake success and stream data is successfully sent
        and received in the presence of packet loss (randomized 25% in each direction).
        """
        data = b"Z" * 65536

        server_configuration = QuicConfiguration(
            is_client=False, quic_logger=QuicLogger()
        )
        server_configuration.load_cert_chain(SERVER_CERTFILE, SERVER_KEYFILE)
        run(self.run_server(configuration=server_configuration))

        response = run(
            self.run_client(
                "127.0.0.1",
                configuration=QuicConfiguration(
                    is_client=True, quic_logger=QuicLogger()
                ),
                request=data,
            )
        )
        self.assertEqual(response, data)

    def test_connect_and_serve_with_session_ticket(self):
        # start server
        client_ticket = None
        store = SessionTicketStore()

        def save_ticket(t):
            nonlocal client_ticket
            client_ticket = t

        run(
            self.run_server(
                session_ticket_fetcher=store.pop, session_ticket_handler=store.add
            )
        )

        # first request
        response = run(
            self.run_client("127.0.0.1", session_ticket_handler=save_ticket),
        )
        self.assertEqual(response, b"gnip")

        self.assertIsNotNone(client_ticket)

        # second request
        run(
            self.run_client(
                "127.0.0.1",
                configuration=QuicConfiguration(
                    is_client=True, session_ticket=client_ticket
                ),
            )
        )
        self.assertEqual(response, b"gnip")

    def test_connect_and_serve_with_sni(self):
        run(self.run_server())
        response = run(self.run_client("localhost"))
        self.assertEqual(response, b"gnip")

    def test_connect_and_serve_with_stateless_retry(self):
        run(self.run_server())
        response = run(self.run_client("127.0.0.1"))
        self.assertEqual(response, b"gnip")

    def test_connect_and_serve_with_stateless_retry_bad_original_connection_id(self):
        """
        If the server's transport parameters do not have the correct
        original_connection_id the connection fail.
        """

        def create_protocol(*args, **kwargs):
            protocol = QuicConnectionProtocol(*args, **kwargs)
            protocol._quic._original_connection_id = None
            return protocol

        run(self.run_server(create_protocol=create_protocol, stateless_retry=True))
        with self.assertRaises(ConnectionError):
            run(self.run_client("127.0.0.1"))

    @patch("aioquic.quic.retry.QuicRetryTokenHandler.validate_token")
    def test_connect_and_serve_with_stateless_retry_bad(self, mock_validate):
        mock_validate.side_effect = ValueError("Decryption failed.")

        run(self.run_server(stateless_retry=True))
        with self.assertRaises(ConnectionError):
            run(
                self.run_client(
                    "127.0.0.1",
                    configuration=QuicConfiguration(is_client=True, idle_timeout=4.0),
                )
            )

    def test_connect_and_serve_with_version_negotiation(self):
        run(self.run_server())

        # force version negotiation
        configuration = QuicConfiguration(is_client=True, quic_logger=QuicLogger())
        configuration.supported_versions.insert(0, 0x1A2A3A4A)

        response = run(self.run_client("127.0.0.1", configuration=configuration))
        self.assertEqual(response, b"gnip")

    def test_connect_timeout(self):
        with self.assertRaises(ConnectionError):
            run(
                self.run_client(
                    "127.0.0.1",
                    port=4400,
                    configuration=QuicConfiguration(is_client=True, idle_timeout=5),
                )
            )

    def test_connect_timeout_no_wait_connected(self):
        async def run_client_no_wait_connected(host, port, configuration):
            configuration.load_verify_locations(cafile=SERVER_CACERTFILE)
            async with connect(
                host, port, configuration=configuration, wait_connected=False
            ) as client:
                await client.ping()

        with self.assertRaises(ConnectionError):
            run(
                run_client_no_wait_connected(
                    "127.0.0.1",
                    port=4400,
                    configuration=QuicConfiguration(is_client=True, idle_timeout=5),
                )
            )

    def test_change_connection_id(self):
        async def run_client_change_connection_id(host, port=4433):
            configuration = QuicConfiguration(is_client=True)
            configuration.load_verify_locations(cafile=SERVER_CACERTFILE)
            async with connect(host, port, configuration=configuration) as client:
                await client.ping()
                client.change_connection_id()
                await client.ping()

        run(self.run_server())
        run(run_client_change_connection_id("127.0.0.1"))

    def test_key_update(self):
        async def run_client_key_update(host, port=4433):
            configuration = QuicConfiguration(is_client=True)
            configuration.load_verify_locations(cafile=SERVER_CACERTFILE)
            async with connect(host, port, configuration=configuration) as client:
                await client.ping()
                client.request_key_update()
                await client.ping()

        run(self.run_server())
        run(run_client_key_update("127.0.0.1"))

    def test_ping(self):
        async def run_client_ping(host, port=4433):
            configuration = QuicConfiguration(is_client=True)
            configuration.load_verify_locations(cafile=SERVER_CACERTFILE)
            async with connect(host, port, configuration=configuration) as client:
                await client.ping()
                await client.ping()

        run(self.run_server())
        run(run_client_ping("127.0.0.1"))

    def test_ping_parallel(self):
        async def run_client_ping(host, port=4433):
            configuration = QuicConfiguration(is_client=True)
            configuration.load_verify_locations(cafile=SERVER_CACERTFILE)
            async with connect(host, port, configuration=configuration) as client:
                coros = [client.ping() for x in range(16)]
                await asyncio.gather(*coros)

        run(self.run_server())
        run(run_client_ping("127.0.0.1"))

    def test_server_receives_garbage(self):
        server = run(self.run_server())
        server.datagram_received(binascii.unhexlify("c00000000080"), ("1.2.3.4", 1234))
        server.close()
