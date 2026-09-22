#include <stdio.h>
#include <stdlib.h>

int main(void) {
    int local = 0;
    void *heap = malloc(16);
    if (!heap) return 1;
    printf("main=%p stack=%p heap=%p\n", (void *)main, (void *)&local, heap);
    free(heap);
    return 0;
}
