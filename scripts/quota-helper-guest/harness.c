#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <linux/dqblk_xfs.h>
#include <linux/fs.h>
#include <linux/quota.h>
#include <linux/stat.h>
#include <signal.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mount.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/types.h>
#include <sys/un.h>
#include <sys/wait.h>
#include <unistd.h>

#define ROOT "/quota/session-1"
#define POLICY "/etc/doxa/session-1.json"
#define SOCK "/run/doxa/quota/session-1.sock"
#define OTHER_SOCK "/run/doxa/quota/other.sock"
#define PROJECT 1002
#define LIMIT (32ULL * 1024 * 1024)
#define OWNER 2002
#define CALLER 2001

static void die(const char *message) {
    fprintf(stderr, "GUEST_FATAL %s errno=%d %s\n", message, errno, strerror(errno));
    exit(1);
}
static void require(bool condition, const char *message) {
    if (!condition) { fprintf(stderr, "GUEST_ASSERT %s\n", message); exit(1); }
}
static void make_dir(const char *path, mode_t mode) {
    if (mkdir(path, mode) && errno != EEXIST) die(path);
    if (chmod(path, mode)) die("chmod directory");
}
static void set_project(const char *path) {
    int fd = open(path, O_RDONLY | O_DIRECTORY | O_CLOEXEC);
    if (fd < 0) die("open project directory");
    struct fsxattr value = {0};
    if (ioctl(fd, FS_IOC_FSGETXATTR, &value)) die("get project xattr");
    value.fsx_projid = PROJECT;
    value.fsx_xflags |= FS_XFLAG_PROJINHERIT;
    if (ioctl(fd, FS_IOC_FSSETXATTR, &value)) die("set project xattr");
    close(fd);
}
static void set_limit(void) {
    int fd = open("/quota", O_RDONLY | O_DIRECTORY | O_CLOEXEC);
    if (fd < 0) die("open quota mount");
    fs_disk_quota_t quota = {0};
    quota.d_version = 1;
    quota.d_flags = FS_PROJ_QUOTA;
    quota.d_fieldmask = FS_DQ_BHARD;
    quota.d_id = PROJECT;
    quota.d_blk_hardlimit = LIMIT / 512;
    if (syscall(SYS_quotactl_fd, fd, QCMD(Q_XSETQLIM, PRJQUOTA), PROJECT, &quota))
        die("set guest fixture limit");
    close(fd);
}
struct binding { uint64_t device, inode, mount_id; };
struct identities { struct binding root, checkout, home, cache, broker; };
static struct binding identity(const char *path) {
    struct stat st;
    struct statx sx = {0};
    if (stat(path, &st)) die("stat fixture binding");
    if (statx(AT_FDCWD, path, AT_SYMLINK_NOFOLLOW, STATX_MNT_ID, &sx)) die("statx fixture binding");
    require((sx.stx_mask & STATX_MNT_ID) != 0, "mount ID missing");
    return (struct binding){ .device = st.st_dev, .inode = st.st_ino, .mount_id = sx.stx_mnt_id };
}
static struct identities capture(void) {
    return (struct identities) { identity(ROOT), identity(ROOT "/checkout"),
        identity(ROOT "/home"), identity(ROOT "/cache"), identity(ROOT "/broker") };
}
static void print_binding(FILE *out, const char *name, struct binding value, bool comma) {
    fprintf(out, "    \"%s\": {\"device\":%llu,\"inode\":%llu,\"mount_id\":%llu}%s\n",
        name, (unsigned long long)value.device, (unsigned long long)value.inode,
        (unsigned long long)value.mount_id, comma ? "," : "");
}
static void write_policy(struct identities bindings, const char *root, uint32_t project,
                         uint64_t limit, int uid, mode_t mode) {
    int fd = open(POLICY, O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, 0600);
    if (fd < 0) die("create policy");
    FILE *out = fdopen(fd, "w");
    if (!out) die("fdopen policy");
    fprintf(out, "{\n  \"version\":1,\"session_id\":\"session-1\",\n"
        "  \"root\":\"%s\",\"socket_path\":\"%s\",\n"
        "  \"caller_uid\":%d,\"owner_uid\":%d,\"project_id\":%u,\"hard_limit_bytes\":%llu,\n"
        "  \"bindings\":{\n", root, SOCK, CALLER, OWNER, project, (unsigned long long)limit);
    print_binding(out, "root", bindings.root, true);
    print_binding(out, "checkout", bindings.checkout, true);
    print_binding(out, "home", bindings.home, true);
    print_binding(out, "cache", bindings.cache, true);
    print_binding(out, "broker", bindings.broker, false);
    fprintf(out, "  }\n}\n");
    if (fclose(out)) die("close policy");
    if (chown(POLICY, uid, uid) || chmod(POLICY, mode)) die("policy metadata");
}
static int listener(const char *path) {
    unlink(path);
    int fd = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
    if (fd < 0) die("create activation listener");
    struct sockaddr_un address = {.sun_family = AF_UNIX};
    strncpy(address.sun_path, path, sizeof(address.sun_path) - 1);
    if (bind(fd, (struct sockaddr *)&address, sizeof(address))) die("bind activation listener");
    if (chown(path, 0, CALLER) || chmod(path, 0660)) die("activation socket metadata");
    if (listen(fd, 8)) die("listen activation socket");
    return fd;
}
static pid_t start_helper_on_listener(int fd) {
    pid_t child = fork();
    if (child < 0) die("fork helper");
    if (child == 0) {
        if (fd != 3) { if (dup2(fd, 3) < 0) die("dup activation FD"); close(fd); }
        int flags = fcntl(3, F_GETFD);
        if (fcntl(3, F_SETFD, flags & ~FD_CLOEXEC)) die("activation CLOEXEC");
        char pid[32]; snprintf(pid, sizeof(pid), "%d", getpid());
        setenv("LISTEN_PID", pid, 1);
        setenv("LISTEN_FDS", "1", 1);
        execl("/doxa-quota-helper", "/doxa-quota-helper", POLICY, NULL);
        die("exec helper");
    }
    close(fd);
    usleep(150000);
    return child;
}
static pid_t start_helper(const char *socket_path) {
    return start_helper_on_listener(listener(socket_path));
}
struct prequeued { pid_t child; int output; };
static struct prequeued prequeue_as(uid_t uid, gid_t gid, int pass_fd) {
    int ready[2], result[2];
    if (pipe(ready) || pipe(result)) die("prequeued client pipes");
    pid_t child = fork(); if (child < 0) die("fork prequeued client");
    if (child == 0) {
        close(ready[0]); close(result[0]);
        if (setresgid(gid, gid, gid) || setresuid(uid, uid, uid)) die("drop prequeued UID");
        int fd = socket(AF_UNIX, SOCK_STREAM, 0);
        struct sockaddr_un address = {.sun_family = AF_UNIX};
        strncpy(address.sun_path, SOCK, sizeof(address.sun_path) - 1);
        if (connect(fd, (struct sockaddr *)&address, sizeof(address))) _exit(2);
        if (pass_fd >= 0) {
            char byte = 'F';
            struct iovec data = {&byte, 1};
            char control[CMSG_SPACE(sizeof(int))] = {0};
            struct msghdr msg = {.msg_iov = &data, .msg_iovlen = 1,
                .msg_control = control, .msg_controllen = sizeof(control)};
            struct cmsghdr *header = CMSG_FIRSTHDR(&msg);
            header->cmsg_level = SOL_SOCKET; header->cmsg_type = SCM_RIGHTS;
            header->cmsg_len = CMSG_LEN(sizeof(int));
            memcpy(CMSG_DATA(header), &pass_fd, sizeof(int));
            if (sendmsg(fd, &msg, 0) != 1) _exit(2);
        }
        if (write(ready[1], "R", 1) != 1) _exit(2);
        close(ready[1]);
        struct timeval deadline = {.tv_sec = 3};
        setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &deadline, sizeof(deadline));
        char buffer[1024]; ssize_t count = read(fd, buffer, sizeof(buffer));
        if (count > 0 && write(result[1], buffer, count) != count) _exit(2);
        close(result[1]); close(fd); _exit(0);
    }
    close(ready[1]); close(result[1]);
    char marker = 0;
    require(read(ready[0], &marker, 1) == 1 && marker == 'R', "prequeued client did not connect");
    close(ready[0]);
    return (struct prequeued){child, result[0]};
}
static void expect_prequeued(const char *label, struct prequeued prequeued, bool verified) {
    char output[1024];
    ssize_t count = read(prequeued.output, output, sizeof(output) - 1);
    if (count < 0) die("read prequeued result");
    output[count] = 0; close(prequeued.output);
    int status = 0;
    require(waitpid(prequeued.child, &status, 0) == prequeued.child && WIFEXITED(status)
        && WEXITSTATUS(status) == 0, "prequeued client process failed");
    bool got = strstr(output, "\"verified\":true") != NULL;
    require(got == verified, label);
    require(strstr(output, "\"admissible_as_hard_quota\":true") == NULL,
        "prequeued client received hardened admission");
    if (verified) require(strstr(output, "\"admissible_as_hard_quota\":false") != NULL,
        "prequeued client received admission");
    printf("GUEST_CASE %s verified=%d reply=%s\n", label, got, output);
}
static void stop_helper(pid_t child, const char *socket_path) {
    kill(child, SIGTERM);
    waitpid(child, NULL, 0);
    unlink(socket_path);
}
static void query_as(uid_t uid, gid_t gid, char *output, size_t capacity, int pass_fd) {
    int pipes[2]; if (pipe(pipes)) die("pipe client");
    pid_t child = fork(); if (child < 0) die("fork client");
    if (child == 0) {
        close(pipes[0]);
        if (setresgid(gid, gid, gid) || setresuid(uid, uid, uid)) die("drop client UID");
        int fd = socket(AF_UNIX, SOCK_STREAM, 0);
        struct sockaddr_un address = {.sun_family = AF_UNIX};
        strncpy(address.sun_path, SOCK, sizeof(address.sun_path) - 1);
        if (connect(fd, (struct sockaddr *)&address, sizeof(address))) {
            if (write(pipes[1], "CONNECT_REFUSED", 15) != 15) _exit(2);
            _exit(0);
        }
        if (pass_fd >= 0) {
            char byte = 'F';
            struct iovec data = {&byte, 1};
            char control[CMSG_SPACE(sizeof(int))] = {0};
            struct msghdr msg = {.msg_iov = &data, .msg_iovlen = 1,
                .msg_control = control, .msg_controllen = sizeof(control)};
            struct cmsghdr *header = CMSG_FIRSTHDR(&msg);
            header->cmsg_level = SOL_SOCKET; header->cmsg_type = SCM_RIGHTS;
            header->cmsg_len = CMSG_LEN(sizeof(int));
            memcpy(CMSG_DATA(header), &pass_fd, sizeof(int));
            if (sendmsg(fd, &msg, 0) != 1) _exit(2);
        }
        struct timeval deadline = {.tv_sec = 3};
        setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &deadline, sizeof(deadline));
        char buffer[1024]; ssize_t count = read(fd, buffer, sizeof(buffer));
        if (count > 0 && write(pipes[1], buffer, count) != count) _exit(2);
        close(fd); close(pipes[1]); _exit(0);
    }
    close(pipes[1]);
    ssize_t count = read(pipes[0], output, capacity - 1);
    if (count < 0) die("read client result");
    output[count] = 0;
    close(pipes[0]);
    int status = 0;
    if (waitpid(child, &status, 0) != child || !WIFEXITED(status) || WEXITSTATUS(status) != 0)
        die("client fixture process failed");
}
static void expect_query(const char *label, pid_t helper, uid_t uid, gid_t gid,
                         bool verified, int pass_fd) {
    char output[1024]; query_as(uid, gid, output, sizeof(output), pass_fd);
    bool got = strstr(output, "\"verified\":true") != NULL;
    bool nonadmit = strstr(output, "\"admissible_as_hard_quota\":false") != NULL;
    require(got == verified, label);
    require(strstr(output, "\"admissible_as_hard_quota\":true") == NULL,
        "client received hardened admission");
    if (verified) require(nonadmit, "helper unexpectedly admitted hardened quota");
    printf("GUEST_CASE %s verified=%d reply=%s\n", label, got, output);
    (void)helper;
}
int main(void) {
    make_dir("/quota", 0755); make_dir("/alias", 0755);
    make_dir("/alias/session-1", 0700);
    make_dir("/etc/doxa", 0755); make_dir("/run/doxa", 0755);
    make_dir("/run/doxa/quota", 0755);
    if (mount("/dev/vda", "/quota", "ext4", 0, "prjquota")) die("mount private ext4 fixture");
    printf("GUEST_MOUNT ext4 prjquota\n");
    make_dir(ROOT, 0700);
    set_project(ROOT);
    for (int index = 0; index < 4; index++) {
        const char *name[] = {"checkout", "home", "cache", "broker"};
        char path[128]; snprintf(path, sizeof(path), "%s/%s", ROOT, name[index]);
        make_dir(path, 0700);
        if (chown(path, OWNER, OWNER)) die("chown bind source");
    }
    if (chown(ROOT, OWNER, OWNER)) die("chown session root");
    set_limit();
    struct identities pinned = capture();
    printf("GUEST_IDENTITIES dev=%llu mount=%llu project=%d limit=%llu\n",
        (unsigned long long)pinned.root.device, (unsigned long long)pinned.root.mount_id,
        PROJECT, (unsigned long long)LIMIT);
    write_policy(pinned, ROOT, PROJECT, LIMIT, 0, 0600);
    pid_t helper = start_helper(SOCK);
    expect_query("exact_policy", helper, CALLER, CALLER, true, -1);
    expect_query("wrong_caller", helper, 2003, CALLER, false, -1);
    expect_query("tree_owner_caller", helper, OWNER, CALLER, false, -1);
    stop_helper(helper, SOCK);

    int activation = listener(SOCK);
    int substituted = open("/quota", O_RDONLY | O_DIRECTORY);
    struct prequeued queued = prequeue_as(CALLER, CALLER, substituted);
    close(substituted);
    helper = start_helper_on_listener(activation);
    expect_prequeued("caller_supplied_fd_ignored", queued, true);
    stop_helper(helper, SOCK);

    activation = listener(SOCK);
    queued = prequeue_as(CALLER, CALLER, -1);
    helper = start_helper_on_listener(activation);
    expect_prequeued("valid_prequeued_caller", queued, true);
    stop_helper(helper, SOCK);

    activation = listener(SOCK);
    queued = prequeue_as(2003, CALLER, -1);
    helper = start_helper_on_listener(activation);
    expect_prequeued("wrong_prequeued_caller", queued, false);
    expect_query("valid_after_wrong_prequeue", helper, CALLER, CALLER, true, -1);
    stop_helper(helper, SOCK);

    write_policy(pinned, ROOT, PROJECT + 1, LIMIT, 0, 0600);
    helper = start_helper(SOCK);
    expect_query("wrong_project", helper, CALLER, CALLER, false, -1);
    stop_helper(helper, SOCK);

    write_policy(pinned, ROOT, PROJECT, LIMIT + 512, 0, 0600);
    helper = start_helper(SOCK);
    expect_query("wrong_limit", helper, CALLER, CALLER, false, -1);
    stop_helper(helper, SOCK);

    struct identities altered = pinned; altered.cache.inode += 100000;
    write_policy(altered, ROOT, PROJECT, LIMIT, 0, 0600);
    helper = start_helper(SOCK);
    expect_query("wrong_bind_inode", helper, CALLER, CALLER, false, -1);
    stop_helper(helper, SOCK);

    write_policy(pinned, ROOT, PROJECT, LIMIT, 0, 0600);
    helper = start_helper(OTHER_SOCK);
    int status = 0;
    require(waitpid(helper, &status, WNOHANG) == helper && WIFEXITED(status)
        && WEXITSTATUS(status) != 0, "substituted activation listener was accepted");
    unlink(OTHER_SOCK);
    printf("GUEST_CASE wrong_activation_fd refused=1\n");

    int original = listener(SOCK);
    queued = prequeue_as(CALLER, CALLER, -1);
    if (rename(SOCK, SOCK ".orphan")) die("move original activation pathname");
    int decoy = listener(SOCK);
    helper = fork();
    if (helper < 0) die("fork rebound helper");
    if (helper == 0) {
        if (original != 3) { if (dup2(original, 3) < 0) die("dup rebound FD"); close(original); }
        int flags = fcntl(3, F_GETFD);
        if (fcntl(3, F_SETFD, flags & ~FD_CLOEXEC)) die("rebound activation CLOEXEC");
        char pid[32]; snprintf(pid, sizeof(pid), "%d", getpid());
        setenv("LISTEN_PID", pid, 1); setenv("LISTEN_FDS", "1", 1);
        execl("/doxa-quota-helper", "/doxa-quota-helper", POLICY, NULL);
        die("exec rebound helper");
    }
    close(original);
    usleep(1200000);
    require(waitpid(helper, &status, WNOHANG) == helper && WIFEXITED(status)
        && WEXITSTATUS(status) != 0, "root-owned pathname rebound was accepted");
    expect_prequeued("rebound_prequeued_caller_closed", queued, false);
    close(decoy);
    unlink(SOCK); unlink(SOCK ".orphan");
    printf("GUEST_CASE root_owned_path_rebound refused=1\n");

    helper = start_helper(SOCK);
    if (kill(helper, SIGSTOP)) die("pause helper before pathname replacement");
    queued = prequeue_as(CALLER, CALLER, -1);
    if (rename(SOCK, SOCK ".orphan")) die("move active listener pathname");
    decoy = listener(SOCK);
    if (kill(helper, SIGCONT)) die("resume helper after pathname replacement");
    usleep(150000);
    require(waitpid(helper, &status, WNOHANG) == helper && WIFEXITED(status)
        && WEXITSTATUS(status) != 0, "post-activation pathname replacement was accepted");
    expect_prequeued("queued_client_closed_after_rebound", queued, false);
    close(decoy); unlink(SOCK); unlink(SOCK ".orphan");
    printf("GUEST_CASE path_rebound_after_activation refused=1\n");

    if (rename(POLICY, POLICY ".original")) die("save policy for symlink substitution");
    if (symlink(POLICY ".original", POLICY)) die("substitute policy symlink");
    helper = start_helper(SOCK);
    expect_query("policy_symlink", helper, CALLER, CALLER, false, -1);
    stop_helper(helper, SOCK);
    unlink(POLICY);
    if (rename(POLICY ".original", POLICY)) die("restore policy");

    altered = pinned;
    altered.root.mount_id++; altered.checkout.mount_id++; altered.home.mount_id++;
    altered.cache.mount_id++; altered.broker.mount_id++;
    write_policy(altered, ROOT, PROJECT, LIMIT, 0, 0600);
    helper = start_helper(SOCK);
    expect_query("wrong_mount_id", helper, CALLER, CALLER, false, -1);
    stop_helper(helper, SOCK);

    if (mount(ROOT, "/alias/session-1", NULL, MS_BIND, NULL)) die("bind alias fixture");
    write_policy(pinned, "/alias/session-1", PROJECT, LIMIT, 0, 0600);
    helper = start_helper(SOCK);
    expect_query("same_inode_different_mount", helper, CALLER, CALLER, false, -1);
    stop_helper(helper, SOCK);
    if (umount("/alias/session-1")) die("unmount alias fixture");

    write_policy(pinned, ROOT, PROJECT, LIMIT, 0, 0644);
    helper = start_helper(SOCK);
    expect_query("world_readable_policy", helper, CALLER, CALLER, false, -1);
    stop_helper(helper, SOCK);

    write_policy(pinned, ROOT, PROJECT, LIMIT, CALLER, 0600);
    helper = start_helper(SOCK);
    expect_query("caller_owned_policy", helper, CALLER, CALLER, false, -1);
    stop_helper(helper, SOCK);

    write_policy(pinned, ROOT, PROJECT, LIMIT, 0, 0600);
    if (rename(ROOT "/cache", ROOT "/cache-old")) die("rename bind source");
    make_dir(ROOT "/cache", 0700);
    if (chown(ROOT "/cache", OWNER, OWNER)) die("chown replacement cache");
    helper = start_helper(SOCK);
    expect_query("replaced_bind_directory", helper, CALLER, CALLER, false, -1);
    stop_helper(helper, SOCK);

    printf("DOXA_QUOTA_HELPER_GUEST_PASS cases=21 admission=false\n");
    return 0;
}
