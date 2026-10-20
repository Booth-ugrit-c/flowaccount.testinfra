[CmdletBinding()]
param(
    [Parameter(Mandatory)][ValidateRange(4, 64)][int]$MemoryGiB,
    [string]$VMName = 'pve01'
)
$ErrorActionPreference = 'Stop'

$vm = Get-VM -Name $VMName
if ($vm.State -ne 'Off') {
    [Console]::Error.WriteLine("refusing to resize ${VMName}: state is $($vm.State), it must be Off")
    exit 2
}
if ($vm.DynamicMemoryEnabled) {
    [Console]::Error.WriteLine("refusing to resize ${VMName}: dynamic memory is on, the trials assume static memory")
    exit 2
}
$before = $vm.MemoryStartup / 1GB
Set-VM -Name $VMName -MemoryStartupBytes ([int64]$MemoryGiB * 1GB)
$after = (Get-VM -Name $VMName).MemoryStartup / 1GB
"{0}: memory {1} GiB -> {2} GiB" -f $VMName, $before, $after
if ($after -ne $MemoryGiB) { exit 1 }
