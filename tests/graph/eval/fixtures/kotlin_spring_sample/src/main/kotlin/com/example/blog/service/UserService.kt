package com.example.blog.service

import com.example.blog.entity.User
import com.example.blog.repository.UserRepository
import org.springframework.stereotype.Service

@Service
class UserService(private val userRepository: UserRepository) {

    fun register(email: String, name: String): User {
        val user = User()
        user.email = email
        user.name = name
        return userRepository.save(user)
    }

    fun get(id: Long): User? = userRepository.findById(id).orElse(null)
}
