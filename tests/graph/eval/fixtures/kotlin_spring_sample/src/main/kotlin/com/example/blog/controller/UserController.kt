package com.example.blog.controller

import com.example.blog.entity.User
import com.example.blog.service.UserService
import org.springframework.web.bind.annotation.GetMapping
import org.springframework.web.bind.annotation.PathVariable
import org.springframework.web.bind.annotation.PostMapping
import org.springframework.web.bind.annotation.RequestBody
import org.springframework.web.bind.annotation.RequestMapping
import org.springframework.web.bind.annotation.RestController

data class RegisterPayload(val email: String, val name: String)

@RestController
@RequestMapping("/api/users")
class UserController(private val userService: UserService) {

    @PostMapping
    fun register(@RequestBody payload: RegisterPayload): User =
        userService.register(payload.email, payload.name)

    @GetMapping("/{id}")
    fun read(@PathVariable id: Long): User? = userService.get(id)
}
