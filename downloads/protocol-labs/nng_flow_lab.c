#include <nng/nng.h>
#include <nng/protocol/pipeline0/pull.h>
#include <nng/protocol/pipeline0/push.h>
#include <nng/protocol/pubsub0/pub.h>
#include <nng/protocol/pubsub0/sub.h>

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static void
fail(int rv, const char *operation)
{
    if (rv != 0) {
        fprintf(stderr, "%s: %s (%d)\n", operation, nng_strerror(rv), rv);
        exit(1);
    }
}

static void
push_backpressure(void)
{
    nng_socket push;
    int rv;

    fail(nng_push0_open(&push), "push open");
    fail(nng_socket_set_int(push, NNG_OPT_SENDBUF, 0), "push sendbuf");
    fail(nng_socket_set_ms(push, NNG_OPT_SENDTIMEO, 80), "push timeout");
    rv = nng_send(push, "job", 3, 0);
    printf("PUSH with no PULL: %s\n", nng_strerror(rv));
    if (rv != NNG_ETIMEDOUT) {
        fprintf(stderr, "expected PUSH timeout\n");
        exit(1);
    }
    fail(nng_close(push), "push close");
}

static void
pubsub_prefix_and_loss(void)
{
    const char *url = "inproc://nng-depth-pubsub";
    const unsigned char prefix[] = { 0x01, 0x02 };
    const unsigned char before_pipe[] = { 0x01, 0x02, 0x00 };
    const unsigned char match[] = { 0x01, 0x02, 0xaa };
    const unsigned char miss[] = { 0x01, 0x03, 0xbb };
    nng_socket pub;
    nng_socket sub;
    void *data = NULL;
    size_t size = 0;
    int rv;

    fail(nng_pub0_open(&pub), "pub open");
    fail(nng_sub0_open(&sub), "sub open");
    fail(nng_socket_set(sub, NNG_OPT_SUB_SUBSCRIBE, prefix, sizeof(prefix)),
        "binary subscribe");
    fail(nng_socket_set_ms(sub, NNG_OPT_RECVTIMEO, 100), "sub timeout");
    fail(nng_listen(pub, url, NULL, 0), "pub listen");

    /* PUB is best effort: success here does not imply any subscriber existed. */
    fail(nng_send(pub, (void *) before_pipe, sizeof(before_pipe), 0),
        "pub before subscriber");
    fail(nng_dial(sub, url, NULL, 0), "sub dial");
    rv = nng_recv(sub, &data, &size, NNG_FLAG_ALLOC);
    printf("message sent before SUB pipe: %s\n", nng_strerror(rv));
    if (rv != NNG_ETIMEDOUT) {
        fprintf(stderr, "unexpected pre-connect PUB delivery\n");
        exit(1);
    }

    fail(nng_send(pub, (void *) match, sizeof(match), 0), "matching publish");
    fail(nng_recv(sub, &data, &size, NNG_FLAG_ALLOC), "matching receive");
    printf("binary prefix 01 02 matched: %02x %02x %02x\n",
        ((unsigned char *) data)[0], ((unsigned char *) data)[1],
        ((unsigned char *) data)[2]);
    nng_free(data, size);
    data = NULL;

    fail(nng_send(pub, (void *) miss, sizeof(miss), 0), "nonmatching publish");
    rv = nng_recv(sub, &data, &size, NNG_FLAG_ALLOC);
    printf("binary prefix 01 03 rejected: %s\n", nng_strerror(rv));
    if (rv != NNG_ETIMEDOUT) {
        fprintf(stderr, "nonmatching prefix was delivered\n");
        exit(1);
    }

    fail(nng_close(sub), "sub close");
    fail(nng_close(pub), "pub close");
}

int
main(void)
{
    printf("NNG %s\n", nng_version());
    push_backpressure();
    pubsub_prefix_and_loss();
    return 0;
}
