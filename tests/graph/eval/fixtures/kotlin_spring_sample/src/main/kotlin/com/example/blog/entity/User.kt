package com.example.blog.entity

import jakarta.persistence.Column
import jakarta.persistence.Entity
import jakarta.persistence.GeneratedValue
import jakarta.persistence.Id
import jakarta.persistence.OneToMany
import jakarta.persistence.Table

@Entity
@Table(name = "users")
class User {
    @Id
    @GeneratedValue
    var id: Long? = null

    @Column(unique = true)
    var email: String = ""

    @Column
    var name: String = ""

    @OneToMany(mappedBy = "author")
    var posts: MutableList<Post> = mutableListOf()
}
