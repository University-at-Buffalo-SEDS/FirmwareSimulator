#include <stddef.h>
#include <stdint.h>
#include "tlsf.h"
static uint64_t pool[4096];
static void *blocks[2048];
volatile uint32_t result;
volatile uint32_t count;
int printf(const char *format,...){(void)format;return 0;}
void *memcpy(void *dst,const void *src,size_t n){unsigned char *d=dst;const unsigned char *s=src;while(n--)*d++=*s++;return dst;}
void *memset(void *dst,int v,size_t n){unsigned char *d=dst;while(n--)*d++=(unsigned char)v;return dst;}
void Reset_Handler(void){
 tlsf_t t=tlsf_create_with_pool(pool,sizeof(pool));
 if(sizeof(void*)!=4 || tlsf_align_size()!=4){result=1;goto done;}
 while(count<2048 && (blocks[count]=tlsf_malloc(t,16))) count++;
 if(count<100 || count==2048){result=2;goto done;}
 for(unsigned i=0;i<count;i+=2)tlsf_free(t,blocks[i]);
 if(tlsf_memalign(t,8,10)!=NULL){result=3;goto done;}
 void *small=tlsf_malloc(t,10);
 if(!small){result=4;goto done;}
 tlsf_free(t,small);
 for(unsigned i=1;i<count;i+=2)tlsf_free(t,blocks[i]);
 if(!tlsf_malloc(t,20000)){result=5;goto done;}
 result=0x51edcafe;
done: while(1){}
}
__attribute__((section(".isr_vector"),used)) const uintptr_t vectors[]={0x20018000,(uintptr_t)Reset_Handler};
