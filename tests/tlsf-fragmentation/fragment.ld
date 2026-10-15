ENTRY(Reset_Handler)
MEMORY {
 FLASH (rx) : ORIGIN = 0x08000000, LENGTH = 512K
 RAM (rwx) : ORIGIN = 0x20000000, LENGTH = 96K
}
SECTIONS {
 .isr_vector : { KEEP(*(.isr_vector)) } > FLASH
 .text : { *(.text*) *(.rodata*) } > FLASH
 .data : { *(.data*) } > RAM AT> FLASH
 .bss (NOLOAD) : { *(.bss*) *(COMMON) } > RAM
}
