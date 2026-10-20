[CmdletBinding()]
param(
    [string]$VMName = 'pve01',
    [ValidateRange(0, 16)][int]$MarginGiB = 2
)
$ErrorActionPreference = 'Stop'

$vm = Get-VM -Name $VMName
if ($vm.State -ne 'Off') {
    "{0} is already {1}" -f $VMName, $vm.State
    exit 0
}
$needGiB = $vm.MemoryStartup / 1GB + $MarginGiB
$freeGiB = [math]::Round((Get-CimInstance Win32_OperatingSystem).FreePhysicalMemory / 1MB, 1)
if ($freeGiB -lt $needGiB) {
    [Console]::Error.WriteLine(("refusing to start {0}: {1} GiB free, {2} GiB needed ({3} GiB VM + {4} GiB margin)" -f $VMName, $freeGiB, $needGiB, ($vm.MemoryStartup / 1GB), $MarginGiB))
    exit 2
}
Start-VM -Name $VMName
"{0} started with {1} GiB; host had {2} GiB free" -f $VMName, ($vm.MemoryStartup / 1GB), $freeGiB
