/* pam_harness CONFDIR SERVICE USER PASSWORD: run auth + setcred, print the result. */
#include <security/pam_appl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
static const char *password;
static int conv(int n, const struct pam_message **m, struct pam_response **r, void *d)
{
    (void)d;
    *r = calloc((size_t)n, sizeof **r);
    for (int i = 0; i < n; i++) {
        if (m[i]->msg_style == PAM_PROMPT_ECHO_OFF) {
            printf("PROMPT\n");
            (*r)[i].resp = strdup(password);
        } else
            printf("MSG %s\n", m[i]->msg);
    }
    return PAM_SUCCESS;
}
int main(int c, char **v)
{
    struct pam_conv pc = { conv, NULL };
    pam_handle_t *h;
    int rc, cr = -1;
    if (c != 5 && c != 6) return 2;
    password = v[4];
    if (pam_start_confdir(v[2], v[3], &pc, v[1], &h) != PAM_SUCCESS) return 2;
    if (c == 6) pam_set_item(h, PAM_RHOST, v[5]);
    for (int tries = getenv("HARNESS_TRIES") ? atoi(getenv("HARNESS_TRIES")) : 1; tries > 0; tries--) {
        rc = pam_authenticate(h, getenv("HARNESS_SILENT") ? PAM_SILENT : 0);   /* as sudo does */
        if (rc == PAM_SUCCESS)
            break;
    }
    if (rc == PAM_SUCCESS) cr = pam_setcred(h, PAM_ESTABLISH_CRED);
    printf("AUTH %s\nCRED %d\n", rc == PAM_SUCCESS ? "yes" : "no", cr);
    pam_end(h, rc);
    return rc != PAM_SUCCESS;
}
