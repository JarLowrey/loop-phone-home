class_name TacticalSim
extends RefCounted
## A fight, for the driver's tests: only the function names matter.

signal fight_ended(winner_id: StringName, reason: StringName)


func step() -> void:
	pass


func add_ship(ship) -> void:
	pass


func end_fight(winner: StringName, reason: StringName) -> void:
	pass


func _run_interiors() -> void:
	pass


func _remove_dead(ship) -> void:
	pass


func _move_crew(ship) -> void:
	pass


func _check_pair(observer, contact) -> void:
	pass
