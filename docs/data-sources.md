# Battery data sources

The collector prefers the system UPower service and falls back to Linux power
supply attributes under `/sys/class/power_supply`. Peripheral batteries whose
scope is `Device`, or whose UPower `PowerSupply` property is false, are ignored.

Detailed field mappings and diagnostics will be added with the source adapter.

