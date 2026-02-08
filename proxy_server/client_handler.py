from http_parser import parse_http_request
import socket

def handle_client(client_socket):
    try:
        request = client_socket.recv(8192).decode(errors="ignore")
        if not request:
            client_socket.close()
            return

        method, url, host, port = parse_http_request(request)

        if not host:
            client_socket.close()
            return

        # CONNECT TO REAL DESTINATION
        server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_socket.connect((host, port))

        server_socket.sendall(request.encode())

        while True:
            response = server_socket.recv(8192)
            if not response:
                break
            client_socket.sendall(response)

        server_socket.close()
        client_socket.close()

    except Exception as e:
        print("Upstream error:", e)
        client_socket.close()
