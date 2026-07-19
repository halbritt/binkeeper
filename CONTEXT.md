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
Immutable evidence that the owner physically found (`fetch`) or failed to find
(`not_found`) a bin at a site; it re-confirms or shocks location confidence and
never relocates the bin.
_Avoid_: Not-found flag, retrieval log

**Current location**:
The latest location projected by folding a bin's ordered move events; it is not
stored as editable truth.
_Avoid_: Location field

**Profile snapshot**:
Immutable owner-reviewed evidence declaring the current value of one or more
profile keys, including explicit clears.
_Avoid_: Bin edit

**Authority**:
The single system permitted to append new physical-inventory evidence. Engram
holds authority until the verified cutover; BinKeeper holds it afterward.
_Avoid_: Primary copy, dual writer
