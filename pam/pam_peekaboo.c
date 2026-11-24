/*
 * pam_peekaboo: thin client for peekabood.
 *
 *   auth [success=end default=ignore] pam_peekaboo.so
 *   auth optional                     pam_peekaboo.so note_password
 *
 * The first form asks the daemon to recognise the user. The second, placed
 * after the primary block, reports that a password was accepted, which clears
 * the lockout. Any error is PAM_AUTHINFO_UNAVAIL, so the password prompt follows.
 *
 * In a terminal the password can be typed while the camera looks: it becomes
 * PAM_AUTHTOK, which pam_unix tries first. An empty line goes to the prompt.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <syslog.h>
#include <termios.h>
#include <time.h>
#include <unistd.h>

#define PAM_SM_AUTH
#include <security/pam_ext.h>
#include <security/pam_modules.h>

#ifndef PEEKABOO_SOCKET
#define PEEKABOO_SOCKET "/run/peekaboo/peekaboo.sock"
#endif
#ifndef PEEKABOO_SERVER_UID
#define PEEKABOO_SERVER_UID 0
#endif
#define MATCHED "peekaboo_matched"
#define TRIED "peekaboo_tried"
#define DEADLINE_MS 20000
#define NOTE_DEADLINE_MS 3000

/* Names go into a JSON string, so anything needing escapes is rejected. */
static bool plain(const char *s)
{
    size_t n = strlen(s);
    return n > 0 && n <= 64 &&
           strspn(s, "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
                     "0123456789._@$-") == n;
}

static long now_ms(void)
{
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return t.tv_sec * 1000L + t.tv_nsec / 1000000L;
}

static int connect_daemon(pam_handle_t *pamh)
{
    struct sockaddr_un addr = { .sun_family = AF_UNIX };
    struct ucred cred;
    socklen_t len = sizeof cred;
    int fd;

    _Static_assert(sizeof PEEKABOO_SOCKET <= sizeof addr.sun_path, "socket path too long");
    strcpy(addr.sun_path, PEEKABOO_SOCKET);
    fd = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
    if (fd < 0)
        return -1;
    if (connect(fd, (struct sockaddr *)&addr, sizeof addr) < 0) {
        close(fd);
        return -1;
    }
    /* Only believe an answer that comes from root's socket. */
    if (getsockopt(fd, SOL_SOCKET, SO_PEERCRED, &cred, &len) < 0 ||
        cred.uid != PEEKABOO_SERVER_UID) {
        pam_syslog(pamh, LOG_ERR, "socket is not owned by the peekaboo service");
        close(fd);
        return -1;
    }
    return fd;
}

static bool send_all(int fd, const char *buf, size_t len)
{
    while (len > 0) {
        ssize_t n = send(fd, buf, len, MSG_NOSIGNAL);
        if (n < 0 && errno == EINTR)
            continue;
        if (n <= 0)
            return false;
        buf += n;
        len -= (size_t)n;
    }
    return true;
}

/*
 * PAM_SILENT is deliberately not consulted. sudo sets it for every plain command,
 * yet the user has to know to look up and turn their head; like a password
 * prompt, that is part of authenticating. The daemon decides when to keep quiet.
 * Control characters would let text reach the terminal as escape sequences.
 */
static void show(pam_handle_t *pamh, char *text)
{
    for (char *p = text; *p; p++)
        if ((unsigned char)*p < 0x20 || *p == 0x7f)
            *p = ' ';
    pam_info(pamh, "%s", text);
}

/* ---- typing the password meanwhile ---------------------------------------- */

struct typing {
    int tty;                    /* -1 when not in use */
    struct termios saved;
    sigset_t watched, before;
};

/* Only for programs that own a terminal: daemons and dialogs are left alone. */
static void typing_start(pam_handle_t *pamh, struct typing *t)
{
    static const char *const services[] = { "sudo", "sudo-i", "su", "su-l", "doas" };
    const void *service = NULL;
    struct termios quiet;
    bool known = false;

    t->tty = -1;
    if (pam_get_item(pamh, PAM_SERVICE, &service) != PAM_SUCCESS || service == NULL)
        return;
    for (size_t i = 0; i < sizeof services / sizeof *services; i++)
        known |= strcmp(service, services[i]) == 0;
    if (!known)
        return;
    t->tty = open("/dev/tty", O_RDWR | O_NOCTTY | O_CLOEXEC);
    if (t->tty < 0)
        return;
    if (tcgetpgrp(t->tty) != getpgrp() || tcgetattr(t->tty, &t->saved) < 0 ||
        !(t->saved.c_lflag & ICANON)) {
        close(t->tty);
        t->tty = -1;
        return;
    }
    /* Held back until the terminal is as it was: dying in between would leave
     * the user's shell without echo. */
    sigemptyset(&t->watched);
    sigaddset(&t->watched, SIGINT);
    sigaddset(&t->watched, SIGQUIT);
    sigaddset(&t->watched, SIGTSTP);
    sigaddset(&t->watched, SIGTERM);
    sigaddset(&t->watched, SIGHUP);
    sigprocmask(SIG_BLOCK, &t->watched, &t->before);
    quiet = t->saved;
    quiet.c_lflag &= ~(tcflag_t)ECHO;
    quiet.c_lflag |= ECHONL;
    tcsetattr(t->tty, TCSADRAIN, &quiet);
}

static void typing_stop(struct typing *t, bool flush)
{
    if (t->tty < 0)
        return;
    if (flush)                  /* half a password must not land in the shell */
        tcflush(t->tty, TCIFLUSH);
    tcsetattr(t->tty, TCSADRAIN, &t->saved);
    close(t->tty);
    t->tty = -1;
    sigprocmask(SIG_SETMASK, &t->before, NULL);     /* delivers what was held back */
}

static bool interrupted(const struct typing *t)
{
    sigset_t pending;

    if (t->tty < 0 || sigpending(&pending) < 0)
        return false;
    return sigismember(&pending, SIGINT) || sigismember(&pending, SIGQUIT) ||
           sigismember(&pending, SIGTSTP) || sigismember(&pending, SIGTERM) ||
           sigismember(&pending, SIGHUP);
}

/* A line was typed: keep it as the password, unless it is empty. */
static void typing_take(pam_handle_t *pamh, struct typing *t)
{
    char line[PAM_MAX_RESP_SIZE];
    ssize_t n = read(t->tty, line, sizeof line);

    if (n > 1 && line[n - 1] == '\n') {
        line[n - 1] = '\0';
        if (strlen(line) == (size_t)(n - 1))
            pam_set_item(pamh, PAM_AUTHTOK, line);
    }
    explicit_bzero(line, sizeof line);
}

/* Returns true once the daemon says OK. */
static bool converse(pam_handle_t *pamh, int fd, bool quiet, struct typing *typing, long budget)
{
    char buf[1024];
    size_t used = 0;
    long deadline = now_ms() + budget;
    bool hinted = false;

    for (;;) {
        char *eol;
        long left = deadline - now_ms();
        struct pollfd p[2] = { { .fd = fd, .events = POLLIN },
                               { .fd = typing->tty, .events = POLLIN } };    /* -1: ignored */
        ssize_t n;
        int ready;

        if (left <= 0 || used == sizeof buf - 1 || interrupted(typing))
            return false;
        ready = poll(p, 2, (int)(typing->tty >= 0 && left > 100 ? 100 : left));
        if (ready < 0 && errno == EINTR)
            continue;
        if (ready < 0)
            return false;
        if (p[1].revents & POLLIN) {
            typing_take(pamh, typing);
            return false;
        }
        if (p[1].revents != 0 || (ready > 0 && p[0].revents == 0))
            return false;
        if (ready == 0)
            continue;
        n = recv(fd, buf + used, sizeof buf - 1 - used, 0);
        if (n < 0 && errno == EINTR)
            continue;
        if (n <= 0)
            return false;
        used += (size_t)n;
        buf[used] = '\0';

        while ((eol = strchr(buf, '\n')) != NULL) {
            size_t rest = used - (size_t)(eol + 1 - buf);
            *eol = '\0';
            if (strcmp(buf, "OK") == 0 || strncmp(buf, "OK ", 3) == 0)
                return true;
            if (strncmp(buf, "FAIL ", 5) == 0) {
                char *code = buf + 5, *text = strchr(code, ' ');
                if (text != NULL) {
                    *text++ = '\0';
                    pam_syslog(pamh, LOG_INFO, "no match: %s", code);
                    /* Routine reasons stay quiet: the password prompt is enough. */
                    if (!quiet && strcmp(code, "not_enrolled") != 0 && strcmp(code, "denied") != 0 &&
                        strcmp(code, "lid") != 0)
                        show(pamh, text);
                }
                return false;
            }
            if (!quiet && strncmp(buf, "INFO ", 5) == 0) {
                if (typing->tty >= 0 && !hinted) {
                    char hint[sizeof buf + 32];
                    snprintf(hint, sizeof hint, "%s, or type your password", buf + 5);
                    show(pamh, hint);
                    hinted = true;
                } else {
                    show(pamh, buf + 5);
                }
            }
            memmove(buf, eol + 1, rest + 1);
            used = rest;
        }
    }
}

static bool remote(pam_handle_t *pamh)
{
    static const char *const vars[] = { "SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY" };
    const void *host = NULL;

    if (pam_get_item(pamh, PAM_RHOST, &host) == PAM_SUCCESS && host != NULL &&
        *(const char *)host != '\0' && strcmp(host, "localhost") != 0)
        return true;
    for (size_t i = 0; i < sizeof vars / sizeof *vars; i++)
        if (getenv(vars[i]) != NULL || pam_getenv(pamh, vars[i]) != NULL)
            return true;
    return false;
}

static int request(pam_handle_t *pamh, bool quiet, const char *op, long budget)
{
    struct typing typing = { .tty = -1 };
    const char *user = NULL;
    const void *service = NULL;
    char line[256];
    bool ok;
    int fd, len;

    if (pam_get_user(pamh, &user, NULL) != PAM_SUCCESS || user == NULL || !plain(user))
        return PAM_AUTHINFO_UNAVAIL;
    if (pam_get_item(pamh, PAM_SERVICE, &service) != PAM_SUCCESS || service == NULL ||
        !plain(service))
        return PAM_AUTHINFO_UNAVAIL;
    len = snprintf(line, sizeof line, "{\"op\":\"%s\",\"user\":\"%s\",\"service\":\"%s\"}\n",
                   op, user, (const char *)service);
    if (len < 0 || (size_t)len >= sizeof line)
        return PAM_AUTHINFO_UNAVAIL;
    fd = connect_daemon(pamh);
    if (fd < 0)
        return PAM_AUTHINFO_UNAVAIL;
    if (!quiet)
        typing_start(pamh, &typing);
    ok = send_all(fd, line, (size_t)len) && converse(pamh, fd, quiet, &typing, budget);
    close(fd);
    typing_stop(&typing, ok);
    return ok ? PAM_SUCCESS : PAM_AUTHINFO_UNAVAIL;
}

PAM_EXTERN int pam_sm_authenticate(pam_handle_t *pamh, int flags, int argc, const char **argv)
{
    const void *data = NULL;
    int rc;

    (void)flags;
    if (argc > 0 && strcmp(argv[0], "note_password") == 0) {
        /* Only a typed password counts, not our own match or a smart card. */
        if (pam_get_data(pamh, MATCHED, &data) == PAM_SUCCESS && data != NULL)
            return PAM_IGNORE;
        if (pam_get_item(pamh, PAM_AUTHTOK, &data) != PAM_SUCCESS || data == NULL ||
            *(const char *)data == '\0')
            return PAM_IGNORE;
        request(pamh, true, "note_password", NOTE_DEADLINE_MS);
        return PAM_IGNORE;
    }

    if (remote(pamh))
        return PAM_AUTHINFO_UNAVAIL;
    /* sudo retries the whole stack after a wrong password. One look is enough:
     * whoever is typing passwords has given up on the camera. */
    if (pam_get_data(pamh, TRIED, &data) == PAM_SUCCESS && data != NULL)
        return PAM_AUTHINFO_UNAVAIL;
    pam_set_data(pamh, TRIED, (void *)"1", NULL);
    rc = request(pamh, false, "auth", DEADLINE_MS);
    if (rc == PAM_SUCCESS)
        pam_set_data(pamh, MATCHED, (void *)"1", NULL);
    else
        pam_set_data(pamh, MATCHED, NULL, NULL);
    return rc;
}

PAM_EXTERN int pam_sm_setcred(pam_handle_t *pamh, int flags, int argc, const char **argv)
{
    (void)pamh; (void)flags; (void)argc; (void)argv;
    return PAM_IGNORE;
}
