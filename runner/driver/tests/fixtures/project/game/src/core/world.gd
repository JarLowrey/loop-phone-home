class_name World
extends RefCounted
## A world, for the driver's tests: only the function names matter.

signal round_ended(winner_id: StringName, reason: StringName)


func step() -> void:
	pass


func add_unit(unit) -> void:
	pass


func end_round(winner: StringName, reason: StringName) -> void:
	pass


func _run_systems() -> void:
	pass


func _remove_dead(unit) -> void:
	pass


func _move_workers(unit) -> void:
	pass


func _check_pair(observer, contact) -> void:
	pass
