#include <nng/nng.h>
#include <nng/protocol/reqrep0/rep.h>
#include <nng/protocol/reqrep0/req.h>

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static void
check(int rv, const char *operation)
{
    if (rv != 0) {
        fprintf(stderr, "%s: %s\n", operation, nng_strerror(rv));
        exit(1);
    }
}

int
main(int argc, char **argv)
{
    nng_socket socket;
    char *reply = NULL;
    size_t size = 0;

    if (argc != 2 || (strcmp(argv[1], "req") && strcmp(argv[1], "rep"))) {
        fprintf(stderr, "usage: %s req|rep\n", argv[0]);
        return 2;
    }
    printf("NNG %s\n", nng_version());
    if (strcmp(argv[1], "req") == 0) {
        check(nng_req0_open(&socket), "req open");
        check(nng_socket_set_ms(socket, NNG_OPT_SENDTIMEO, 5000), "send timeout");
        check(nng_socket_set_ms(socket, NNG_OPT_RECVTIMEO, 5000), "recv timeout");
        check(nng_listen(socket, "tcp://127.0.0.1:18891", NULL, 0), "listen");
        puts("REQ listens on 127.0.0.1:18891");
        fflush(stdout);
        check(nng_send(socket, "ping", 4, 0), "send");
        check(nng_recv(socket, &reply, &size, NNG_FLAG_ALLOC), "recv");
        printf("REQ received %zu bytes: %.*s\n", size, (int) size, reply);
    } else {
        check(nng_rep0_open(&socket), "rep open");
        check(nng_socket_set_ms(socket, NNG_OPT_SENDTIMEO, 5000), "send timeout");
        check(nng_socket_set_ms(socket, NNG_OPT_RECVTIMEO, 5000), "recv timeout");
        check(nng_dial(socket, "tcp://127.0.0.1:18891", NULL, 0), "dial");
        puts("REP dials 127.0.0.1:18891");
        check(nng_recv(socket, &reply, &size, NNG_FLAG_ALLOC), "recv");
        printf("REP received %zu bytes: %.*s\n", size, (int) size, reply);
        check(nng_send(socket, "pong", 4, 0), "send");
        getchar();
    }
    nng_free(reply, size);
    check(nng_close(socket), "close");
    return 0;
}
