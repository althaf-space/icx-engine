package com.example.blog.repository

import com.example.blog.entity.Post
import com.example.blog.entity.User
import org.springframework.data.jpa.repository.JpaRepository
import org.springframework.stereotype.Repository

@Repository
interface PostRepository : JpaRepository<Post, Long> {
    fun findByAuthor(author: User): List<Post>
}
