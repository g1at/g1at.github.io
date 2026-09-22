#include <stdio.h>
#include <string.h>

__attribute__((noinline)) static void copy_arg(const char *s) {
    char buffer[32];
    strcpy(buffer, s);
    puts(buffer);
}

int main(int argc, char **argv) {
    if (argc != 2) {
        fprintf(stderr, "usage: %s TEXT\n", argv[0]);
        return 2;
    }
    copy_arg(argv[1]);
    return 0;
}
