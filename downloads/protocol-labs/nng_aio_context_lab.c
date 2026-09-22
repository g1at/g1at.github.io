#include <nng/nng.h>
#include <nng/protocol/reqrep0/rep.h>
#include <nng/protocol/reqrep0/req.h>
#include <nng/supplemental/util/platform.h>

#include <stdarg.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define WORKER_COUNT 3
#define CLIENT_COUNT 6
#define LAB_URL "tcp://127.0.0.1:18892"

typedef struct server server;

typedef enum {
    WORK_RECV,
    WORK_DELAY,
    WORK_SEND,
    WORK_STOPPED,
} worker_state;

typedef struct {
    int id;
    nng_ctx ctx;
    nng_aio *aio;
    worker_state state;
    nng_msg *processing;
    bool counted_active;
    server *owner;
} worker;

struct server {
    nng_socket socket;
    worker workers[WORKER_COUNT];
    atomic_bool stopping;
    atomic_int active;
    atomic_int max_active;
    nng_mtx *log_lock;
};

typedef struct {
    int id;
    int delay_ms;
    nng_ctx ctx;
    nng_thread *thread;
    server *log_owner;
    int result;
} client_job;

static void
fail(int rv, const char *operation)
{
    if (rv != 0) {
        fprintf(stderr, "%s: %s (%d)\n", operation, nng_strerror(rv), rv);
        exit(1);
    }
}

static void
log_line(server *s, const char *format, ...)
{
    va_list ap;

    nng_mtx_lock(s->log_lock);
    va_start(ap, format);
    vprintf(format, ap);
    va_end(ap);
    putchar('\n');
    fflush(stdout);
    nng_mtx_unlock(s->log_lock);
}

static void
record_active(server *s, int value)
{
    int seen = atomic_load(&s->max_active);

    while (value > seen &&
        !atomic_compare_exchange_weak(&s->max_active, &seen, value)) {
        /* seen is updated by atomic_compare_exchange_weak. */
    }
}

static void
worker_release_messages(worker *w)
{
    nng_msg *msg = nng_aio_get_msg(w->aio);

    if (msg != NULL) {
        nng_aio_set_msg(w->aio, NULL);
        nng_msg_free(msg);
    }
    if (w->processing != NULL) {
        nng_msg_free(w->processing);
        w->processing = NULL;
    }
    if (w->counted_active) {
        atomic_fetch_sub(&w->owner->active, 1);
        w->counted_active = false;
    }
}

static void
worker_submit_recv(worker *w)
{
    if (atomic_load(&w->owner->stopping)) {
        w->state = WORK_STOPPED;
        return;
    }
    w->state = WORK_RECV;
    nng_ctx_recv(w->ctx, w->aio);
}

static void
worker_callback(void *arg)
{
    worker *w = arg;
    server *s = w->owner;
    int rv = nng_aio_result(w->aio);

    if (atomic_load(&s->stopping)) {
        worker_release_messages(w);
        w->state = WORK_STOPPED;
        log_line(s, "worker=%d stop result=%s", w->id, nng_strerror(rv));
        return;
    }

    if (w->state == WORK_RECV) {
        char request[96];
        int request_id = -1;
        int delay_ms = 0;
        int now_active;
        size_t length;

        if (rv != 0) {
            w->state = WORK_STOPPED;
            log_line(s, "worker=%d recv-error=%s", w->id, nng_strerror(rv));
            return;
        }

        w->processing = nng_aio_get_msg(w->aio);
        nng_aio_set_msg(w->aio, NULL);
        length = nng_msg_len(w->processing);
        if (length >= sizeof(request)) {
            length = sizeof(request) - 1;
        }
        memcpy(request, nng_msg_body(w->processing), length);
        request[length] = '\0';
        if (sscanf(request, "id=%d delay=%d", &request_id, &delay_ms) != 2 ||
            delay_ms < 0 || delay_ms > 1000) {
            request_id = -1;
            delay_ms = 0;
        }

        now_active = atomic_fetch_add(&s->active, 1) + 1;
        w->counted_active = true;
        record_active(s, now_active);
        log_line(s, "worker=%d recv id=%d delay=%d active=%d", w->id,
            request_id, delay_ms, now_active);

        nng_msg_clear(w->processing);
        snprintf(request, sizeof(request), "id=%d worker=%d delay=%d",
            request_id, w->id, delay_ms);
        if ((rv = nng_msg_append(w->processing, request, strlen(request))) != 0) {
            log_line(s, "worker=%d append-error=%s", w->id, nng_strerror(rv));
            worker_release_messages(w);
            w->state = WORK_STOPPED;
            return;
        }

        w->state = WORK_DELAY;
        nng_sleep_aio(delay_ms, w->aio);
        return;
    }

    if (w->state == WORK_DELAY) {
        if (w->counted_active) {
            atomic_fetch_sub(&s->active, 1);
            w->counted_active = false;
        }
        if (rv != 0) {
            log_line(s, "worker=%d delay-error=%s", w->id, nng_strerror(rv));
            worker_release_messages(w);
            w->state = WORK_STOPPED;
            return;
        }

        nng_aio_set_msg(w->aio, w->processing);
        w->processing = NULL;
        w->state = WORK_SEND;
        nng_ctx_send(w->ctx, w->aio);
        return;
    }

    if (w->state == WORK_SEND) {
        nng_msg *left = nng_aio_get_msg(w->aio);

        if (rv != 0) {
            log_line(s, "worker=%d send-error=%s retained=%s", w->id,
                nng_strerror(rv), left == NULL ? "no" : "yes");
            worker_release_messages(w);
            w->state = WORK_STOPPED;
            return;
        }

        log_line(s, "worker=%d send-ok msg_on_aio=%s", w->id,
            left == NULL ? "no" : "yes");
        if (left != NULL) {
            /* A successful asynchronous send must transfer ownership. */
            worker_release_messages(w);
            w->state = WORK_STOPPED;
            return;
        }
        worker_submit_recv(w);
    }
}

static void
client_thread(void *arg)
{
    client_job *job = arg;
    char body[96];
    nng_msg *msg = NULL;
    nng_msg *reply = NULL;
    uint64_t started = nng_clock();
    int rv;

    snprintf(body, sizeof(body), "id=%d delay=%d", job->id, job->delay_ms);
    if ((rv = nng_msg_alloc(&msg, 0)) != 0 ||
        (rv = nng_msg_append(msg, body, strlen(body))) != 0) {
        if (msg != NULL) {
            nng_msg_free(msg);
        }
        job->result = rv;
        return;
    }

    rv = nng_ctx_sendmsg(job->ctx, msg, 0);
    if (rv != 0) {
        /* Failed synchronous send leaves ownership with this thread. */
        nng_msg_free(msg);
        job->result = rv;
        return;
    }
    msg = NULL;

    rv = nng_ctx_recvmsg(job->ctx, &reply, 0);
    if (rv == 0) {
        log_line(job->log_owner, "client=%d reply=%.*s elapsed=%llums", job->id,
            (int) nng_msg_len(reply), (char *) nng_msg_body(reply),
            (unsigned long long) (nng_clock() - started));
        nng_msg_free(reply);
    }
    job->result = rv;
}

static void
probe_async_send_failure(void)
{
    nng_socket rep;
    nng_ctx ctx;
    nng_aio *aio;
    nng_msg *msg;
    int rv;

    fail(nng_rep0_open(&rep), "probe rep open");
    fail(nng_ctx_open(&ctx, rep), "probe ctx open");
    fail(nng_aio_alloc(&aio, NULL, NULL), "probe aio alloc");
    fail(nng_msg_alloc(&msg, 0), "probe msg alloc");
    fail(nng_msg_append(msg, "orphan-reply", 12), "probe msg append");
    nng_aio_set_msg(aio, msg);
    nng_ctx_send(ctx, aio);
    nng_aio_wait(aio);
    rv = nng_aio_result(aio);
    msg = nng_aio_get_msg(aio);
    printf("async send before REP receive: %s; message retained=%s\n",
        nng_strerror(rv), msg == NULL ? "no" : "yes");
    if (rv != NNG_ESTATE || msg == NULL) {
        fprintf(stderr, "unexpected async ownership result\n");
        exit(1);
    }
    nng_aio_set_msg(aio, NULL);
    nng_msg_free(msg);
    nng_aio_free(aio);
    fail(nng_ctx_close(ctx), "probe ctx close");
    fail(nng_close(rep), "probe rep close");
}

static void
probe_reply_timeout(void)
{
    const char *url = "inproc://nng-depth-timeout";
    nng_socket req;
    nng_socket rep;
    void *request = NULL;
    size_t request_size = 0;
    void *reply = NULL;
    size_t reply_size = 0;
    int rv;

    fail(nng_rep0_open(&rep), "timeout rep open");
    fail(nng_req0_open(&req), "timeout req open");
    fail(nng_socket_set_ms(rep, NNG_OPT_RECVTIMEO, 1000),
        "timeout rep receive option");
    fail(nng_socket_set_ms(req, NNG_OPT_RECVTIMEO, 120), "timeout recv option");
    fail(nng_socket_set_ms(req, NNG_OPT_SENDTIMEO, 1000), "timeout send option");
    fail(nng_listen(rep, url, NULL, 0), "timeout listen");
    fail(nng_dial(req, url, NULL, 0), "timeout dial");
    fail(nng_send(req, "probe", 5, 0), "timeout request send");
    fail(nng_recv(rep, &request, &request_size, NNG_FLAG_ALLOC),
        "timeout request receive");
    rv = nng_recv(req, &reply, &reply_size, NNG_FLAG_ALLOC);
    printf("REQ waiting for omitted reply: %s\n", nng_strerror(rv));
    if (rv != NNG_ETIMEDOUT) {
        fprintf(stderr, "expected NNG_ETIMEDOUT\n");
        exit(1);
    }
    nng_free(request, request_size);
    fail(nng_close(req), "timeout req close");
    fail(nng_close(rep), "timeout rep close");
}

int
main(void)
{
    static const int delays[CLIENT_COUNT] = { 180, 20, 120, 10, 80, 40 };
    server s;
    nng_socket req;
    client_job jobs[CLIENT_COUNT];
    int i;

    printf("NNG %s\n", nng_version());
    probe_async_send_failure();
    probe_reply_timeout();

    memset(&s, 0, sizeof(s));
    atomic_init(&s.stopping, false);
    atomic_init(&s.active, 0);
    atomic_init(&s.max_active, 0);
    fail(nng_mtx_alloc(&s.log_lock), "log mutex alloc");
    fail(nng_rep0_open(&s.socket), "server rep open");
    fail(nng_listen(s.socket, LAB_URL, NULL, 0), "server listen");

    for (i = 0; i < WORKER_COUNT; i++) {
        worker *w = &s.workers[i];
        w->id = i;
        w->owner = &s;
        fail(nng_ctx_open(&w->ctx, s.socket), "worker ctx open");
        fail(nng_aio_alloc(&w->aio, worker_callback, w), "worker aio alloc");
        worker_submit_recv(w);
    }

    fail(nng_req0_open(&req), "client req open");
    fail(nng_socket_set_ms(req, NNG_OPT_SENDTIMEO, 1000), "client send timeout");
    fail(nng_socket_set_ms(req, NNG_OPT_RECVTIMEO, 2000), "client recv timeout");
    fail(nng_dial(req, LAB_URL, NULL, 0), "client dial");

    for (i = 0; i < CLIENT_COUNT; i++) {
        jobs[i].id = i;
        jobs[i].delay_ms = delays[i];
        jobs[i].log_owner = &s;
        jobs[i].result = -1;
        fail(nng_ctx_open(&jobs[i].ctx, req), "client ctx open");
        fail(nng_thread_create(&jobs[i].thread, client_thread, &jobs[i]),
            "client thread create");
    }

    for (i = 0; i < CLIENT_COUNT; i++) {
        nng_thread_destroy(jobs[i].thread);
        if (jobs[i].result != 0) {
            fprintf(stderr, "client %d: %s\n", i, nng_strerror(jobs[i].result));
            exit(1);
        }
        fail(nng_ctx_close(jobs[i].ctx), "client ctx close");
    }
    fail(nng_close(req), "client req close");

    printf("max concurrent server jobs: %d\n", atomic_load(&s.max_active));
    if (atomic_load(&s.max_active) < 2) {
        fprintf(stderr, "concurrency was not observed\n");
        exit(1);
    }

    atomic_store(&s.stopping, true);
    for (i = 0; i < WORKER_COUNT; i++) {
        nng_aio_stop(s.workers[i].aio);
    }
    for (i = 0; i < WORKER_COUNT; i++) {
        worker_release_messages(&s.workers[i]);
        fail(nng_ctx_close(s.workers[i].ctx), "worker ctx close");
        nng_aio_free(s.workers[i].aio);
    }
    fail(nng_close(s.socket), "server close");
    nng_mtx_free(s.log_lock);
    puts("shutdown: stop-all -> close-contexts -> free-aio (clean)");
    return 0;
}
