# BinKeeper

BinKeeper records physical-inventory evidence and projects the owner's current
view of labeled storage bins.

## Language

**Bin**:
A stable, owner-assigned identity for one physical storage container.
_Avoid_: Record, inventory row

**Move event**:
Immutable evidence that a bin was placed, opened, loaded, arrived, or closed as
part of a physical move.
_Avoid_: Location update

**Retrieval outcome**:
Immutable evidence of what happened when the owner went to a bin: found it and
took something (`fetch`), opened it and took nothing (`browse`), or failed to
find it at the claimed site (`not_found`). Fetch and browse re-confirm
location; not_found shocks it; browse also accrues confusion (passport drift);
none relocates the bin.
_Avoid_: Not-found flag, retrieval log

**Sub-location anchor**:
A LOC- coded label mounted on a shelf, cabinet, or room edge. It exists in the
system only once photographed co-located with a bin. It is witnessed
sub-location evidence, never a physical parent relation or declared location
taxonomy.
_Avoid_: Room, shelf field, location taxonomy

**Physical containment**:
The current fact that one bin is physically inside another bin. It is projected
from immutable owner-attested pack and unpack events into a single-parent,
acyclic graph.
_Avoid_: Parent field, sub-location anchor, editable nesting

**Current location**:
The latest location projected by folding a top-level bin's ordered move events.
A contained bin inherits the current location of its outermost container. It is
never stored as editable truth.
_Avoid_: Location field

**Profile snapshot**:
Immutable owner-reviewed evidence declaring the current value of one or more
profile keys, including explicit clears.
_Avoid_: Bin edit

**Authority**:
The single system permitted to append new physical-inventory evidence. Engram
holds authority until the verified cutover; BinKeeper holds it afterward.
_Avoid_: Primary copy, dual writer
